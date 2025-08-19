import os
import json
import csv
import boto3
from datetime import datetime

s3 = boto3.client("s3")
dynamodb = boto3.resource("dynamodb")
sns = boto3.client("sns")

TABLE_NAME = os.environ.get("TABLE_NAME")
SNS_TOPIC_ARN = os.environ.get("SNS_TOPIC_ARN")

table = dynamodb.Table(TABLE_NAME)

def _to_24h(time_str: str):
    """
    Convert "8:00pm" to ("20:00", 1200).
    Returns (HH:MM, minutes_since_midnight)
    """
    if not time_str:
        raise ValueError("Missing time")
    s = str(time_str).strip().lower().replace(" ", "")
    try:
        dt = datetime.strptime(s, "%I:%M%p")
    except ValueError:
        # fallback: already 24h "HH:MM"
        dt = datetime.strptime(str(time_str).strip(), "%H:%M")
    return dt.strftime("%H:%M"), dt.hour * 60 + dt.minute

def _normalize_record(rec: dict) -> dict:
    performer = str(rec["Performer"]).strip()
    stage = str(rec["Stage"]).strip()
    date = str(rec["Date"]).strip()  # expect YYYY-MM-DD
    start24, start_m = _to_24h(rec["Start"])
    end24, end_m = _to_24h(rec["End"])

    popularity = None
    if "Popularity" in rec and rec["Popularity"] not in ("", None):
        try:
            popularity = int(rec["Popularity"])
        except Exception:
            popularity = None

    performance_sk = f"{date}#{start24}#{stage}"
    date_start = f"{date}#{start24}"

    item = {
        "Performer": performer,            # PK
        "Performance": performance_sk,     # SK
        "Stage": stage,
        "Date": date,
        "StartTime": start24,
        "EndTime": end24,
        "StartMinutes": start_m,
        "EndMinutes": end_m,
        "DateStart": date_start,           # for StageIndex SK
        "StartSort": start24,              # for DateIndex SK
    }
    if popularity is not None:
        item["Popularity"] = popularity
        item["PopularityBucket"] = "POPULARITY"  # for bonus GSI
    return item

def _load_s3_object(bucket, key) -> bytes:
    obj = s3.get_object(Bucket=bucket, Key=key)
    return obj["Body"].read()

def _parse_payload_bytes(key: str, payload: bytes) -> list[dict]:
    key_lower = key.lower()
    text = payload.decode("utf-8-sig")
    if key_lower.endswith(".json"):
        data = json.loads(text)
        if isinstance(data, dict):
            data = data.get("items", [])
        if not isinstance(data, list):
            raise ValueError("JSON must be a list of records")
        return data
    elif key_lower.endswith(".csv"):
        rows = []
        reader = csv.DictReader(text.splitlines())
        for row in reader:
            rows.append(row)
        return rows
    else:
        # try JSON then CSV naively
        try:
            data = json.loads(text)
            if isinstance(data, dict):
                data = data.get("items", [])
            if isinstance(data, list):
                return data
        except Exception:
            pass
        try:
            rows = []
            reader = csv.DictReader(text.splitlines())
            for row in reader:
                rows.append(row)
            if rows:
                return rows
        except Exception:
            pass
        raise ValueError("Unsupported file format. Use .json or .csv")

def _batch_write(items: list[dict]):
    count = 0
    with table.batch_writer(overwrite_by_pkeys=["Performer", "Performance"]) as batch:
        for it in items:
            batch.put_item(Item=it)
            count += 1
    return count

def lambda_handler(event, context):
    """
    Triggered by SQS (carrying S3 event notification).
    For each S3 object, parse records and write to DynamoDB.
    Sends an SNS email on success/failure.
    """
    successes = 0
    files_processed = 0
    errors: list[str] = []

    try:
        for record in event.get("Records", []):
            body = record.get("body", "{}")
            try:
                body_json = json.loads(body)
            except json.JSONDecodeError:
                errors.append(f"Invalid SQS body JSON: {body[:200]}")
                continue

            s3_records = body_json.get("Records", [])
            if not s3_records:
                errors.append("No S3 records in SQS message")
                continue

            for s3rec in s3_records:
                bucket = s3rec["s3"]["bucket"]["name"]
                key = s3rec["s3"]["object"]["key"].replace("+", " ")
                payload = _load_s3_object(bucket, key)
                raw_rows = _parse_payload_bytes(key, payload)

                norm_items = []
                for row in raw_rows:
                    try:
                        item = _normalize_record(row)
                        norm_items.append(item)
                    except Exception as e:
                        errors.append(f"Bad row skipped ({e}): {row}")

                written = _batch_write(norm_items)
                successes += written
                files_processed += 1

        if SNS_TOPIC_ARN:
            subject = f"[Festival Loader] Success: {successes} items"
            if errors:
                subject = f"[Festival Loader] Partial Success: {successes} items, {len(errors)} errors"
            msg = {
                "files_processed": files_processed,
                "items_written": successes,
                "errors": errors[:20],
                "timestamp": datetime.utcnow().isoformat() + "Z",
            }
            sns.publish(TopicArn=SNS_TOPIC_ARN, Subject=subject, Message=json.dumps(msg, indent=2))

        if files_processed == 0 and errors:
            raise RuntimeError("Failed to process any files")

        return {"statusCode": 200, "written": successes, "errors": len(errors)}

    except Exception as e:
        if SNS_TOPIC_ARN:
            sns.publish(
                TopicArn=SNS_TOPIC_ARN,
                Subject="[Festival Loader] FAILURE",
                Message=json.dumps({"error": str(e)}, indent=2),
            )
        raise
