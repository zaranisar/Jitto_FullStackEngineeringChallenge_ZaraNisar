# Jitto_FullStackEngineeringChallenge_ZaraNisar
Full Stack Engineering Challenge Repository for the Full Stack Software Engineering Internship at Jitto

1) Summary

This project ingests festival lineup files (CSV or JSON) dropped into an S3 bucket, parses and normalizes the data in a Lambda function (Python 3.12, boto3 only), writes items to DynamoDB using a single-table design, and publishes a success/partial/failure email via SNS.

Key access patterns supported efficiently:

All performances by a Performer

All performances in a time range on a Date

Details for a Stage at a specific time

Bonus: query performances with Popularity > 80

Why this architecture?
S3 events are routed to SQS before Lambda for buffering, retries, and back-pressure protection (production-ready ingestion). DynamoDB on-demand scales with traffic; SNS gives simple notifications.

                  ┌────────────┐     S3 Event      ┌──────────┐
   Upload file →  │   S3       │ ───────────────▶  │   SQS    │
                  └────────────┘                    └──────────┘
                         │                               │(trigger)
                         ▼                               ▼
                   (event notification)           ┌───────────────┐
                                                 │   Lambda       │
                                                 │ (Python 3.12)  │
                                                 └──────┬─────────┘
                                                        │
                                 writes items           │ publishes result
                                                        │
                                             ┌──────────▼──────────┐
                                             │    DynamoDB          │
                                             │ + GSIs for queries   │
                                             └──────────┬───────────┘
                                                        │
                                                        ▼
                                                    ┌───────┐
                                                    │  SNS  │ → Email
                                                    └───────┘

2) Repository structure
.
├─ cloudformation/
│  └─ o240-stack.yaml          # All AWS infra (S3, SQS, Lambda, DynamoDB, SNS, IAM)
├─ lambda/
│  └─ handler.py               # Python 3.12 Lambda (boto3 only)
├─ sample/
│  ├─ sample.json              # Example dataset (JSON)
│  └─ sample.csv               # Example dataset (CSV)
└─ README.md                   # This file

3) DynamoDB data model & rationale
3.1 Table & attributes

Table (single-table design)

PK (Performer) — string

SK (Performance) — YYYY-MM-DD#HH:MM#Stage

Attributes

Stage — string

Date — YYYY-MM-DD

StartTime — 24h HH:MM

EndTime — 24h HH:MM

StartMinutes / EndMinutes — minutes since midnight (optional helper)

DateStart — YYYY-MM-DD#HH:MM (for Stage+time lookups)

StartSort — HH:MM (for time ordering within a day)

Popularity — integer 1–100 (optional)

PopularityBucket — constant "POPULARITY" (for bonus GSI)

Primary key choice

Performer + Performance lets us Query all shows for a performer quickly and sort by date/time.

The composite Performance includes date and stage to produce idempotent upserts (same item on retry).

3.2 Global Secondary Indexes (GSIs)

DateIndex — PK: Date, SK: StartSort

Efficient time range queries for a given date: StartSort BETWEEN :from AND :to.

StageIndex — PK: Stage, SK: DateStart

Fast lookups of performances by stage on a date and/or exact time.

PopularityIndex (bonus) — PK: PopularityBucket, SK: Popularity

Enables Popularity > 80 queries without scanning.

Result: Each required query is a single-partition DynamoDB Query (no table scans).

4) Lambda behavior

Triggered by SQS messages that contain the S3 event.

Downloads the object, supports JSON list or CSV headered file.

Normalizes times (e.g., "8:00pm" → "20:00", 1200 minutes).

Batch writes to DynamoDB with idempotent upserts (overwrite_by_pkeys=["Performer","Performance"]).

Sends SNS email with counts and up to 20 error lines.

No validation of overlaps by design (per requirements).

5) Security (least privilege IAM)

Lambda execution role permits only what’s needed:

s3:GetObject on the upload bucket

dynamodb:BatchWriteItem, PutItem, DescribeTable on the table

sqs:ReceiveMessage, DeleteMessage, GetQueueAttributes on the ingest queue

sns:Publish to the topic

SQS queue policy restricts SendMessage to your S3 bucket ARN only.
Encryption: SQS & DynamoDB encrypted by default; S3 uses SSE-S3 (AES-256) by default; SNS topics support SSE if desired.
Public access: S3 Block Public Access is assumed on (recommended).
We do not hardcode IAM RoleName to avoid collisions; CloudFormation generates a unique name.

6) Deploy

Tip: Keep the deployment bucket and the stack in the same region (avoids “PermanentRedirect”).
Below shows CloudShell (no local installs). Local instructions follow.

6.1 Deploy with AWS CloudShell (recommended)

Open CloudShell and clone or upload this repo.

Zip the Lambda:

cd lambda
zip ../function.zip handler.py
cd ..


Pick a region and create a deployment bucket in that region (stack will create the upload bucket):

REGION=us-east-2
aws configure set region "$REGION"

TS=$(date +%s)
DEP_BUCKET="dep-bucket-<yourname>-$TS"
UPLOAD_BUCKET="upload-bucket-<yourname>-$TS"
EMAIL="<you@example.com>"

aws s3api create-bucket \
  --bucket "$DEP_BUCKET" \
  --region "$REGION" \
  --create-bucket-configuration LocationConstraint="$REGION"

aws s3 cp function.zip "s3://$DEP_BUCKET/lambda/function.zip"


Create the stack:

aws cloudformation create-stack \
  --stack-name o240-festival \
  --template-body file://cloudformation/o240-stack.yaml \
  --capabilities CAPABILITY_NAMED_IAM \
  --parameters \
    ParameterKey=UploadBucketName,ParameterValue=$UPLOAD_BUCKET \
    ParameterKey=DeploymentBucketName,ParameterValue=$DEP_BUCKET \
    ParameterKey=LambdaZipKey,ParameterValue=lambda/function.zip \
    ParameterKey=EmailAddress,ParameterValue=$EMAIL

aws cloudformation describe-stacks --stack-name o240-festival \
  --query 'Stacks[0].StackStatus' --output text


Wait for CREATE_COMPLETE.

Confirm SNS — open your email and click Confirm subscription.

Kick the pipeline:

UPLOAD_BUCKET=$(aws cloudformation describe-stacks --stack-name o240-festival \
  --query "Stacks[0].Outputs[?OutputKey=='UploadBucketNameOut'].OutputValue" -o text)

aws s3 cp sample/sample.json "s3://$UPLOAD_BUCKET/sample.json"
# or
aws s3 cp sample/sample.csv  "s3://$UPLOAD_BUCKET/sample.csv"

# (optional) watch logs
aws logs tail /aws/lambda/processFestivalData --since 30m --follow

6.2 Deploy from your laptop

Install AWS CLI, configure aws configure with credentials and target region.

Zip Lambda from the lambda/ folder so handler.py is at the zip root:

cd lambda
zip ../function.zip handler.py
cd ..


Create deployment bucket in your chosen region, upload function.zip, and run the same create-stack command as above.

7) Verify with queries

Get outputs:

STACK=o240-festival
TABLE_NAME=$(aws cloudformation describe-stacks --stack-name "$STACK" \
  --query "Stacks[0].Outputs[?OutputKey=='TableNameOut'].OutputValue" -o text)
echo "TABLE_NAME=$TABLE_NAME"


By performer

aws dynamodb query \
  --table-name "$TABLE_NAME" \
  --key-condition-expression "Performer = :p" \
  --expression-attribute-values '{":p":{"S":"Megan Thee Stallion"}}' \
  --output table


Performances on a date (time range) — GSI DateIndex

aws dynamodb query \
  --table-name "$TABLE_NAME" \
  --index-name DateIndex \
  --key-condition-expression "#d = :date AND #t BETWEEN :from AND :to" \
  --expression-attribute-names '{"#d":"Date","#t":"StartSort"}' \
  --expression-attribute-values '{":date":{"S":"2025-07-12"},":from":{"S":"16:00"},":to":{"S":"21:00"}}' \
  --output table


Exact performance (Stage + time) — GSI StageIndex

aws dynamodb query \
  --table-name "$TABLE_NAME" \
  --index-name StageIndex \
  --key-condition-expression "Stage = :s AND DateStart = :ds" \
  --expression-attribute-values '{":s":{"S":"Main Stage"},":ds":{"S":"2025-07-12#20:00"}}' \
  --output table


Bonus: Popularity > 80 — GSI PopularityIndex

aws dynamodb query \
  --table-name "$TABLE_NAME" \
  --index-name PopularityIndex \
  --key-condition-expression "PopularityBucket = :b AND Popularity > :min" \
  --expression-attribute-values '{":b":{"S":"POPULARITY"},":min":{"N":"80"}}' \
  --output table


Get exact item (PK + SK)

aws dynamodb get-item \
  --table-name "$TABLE_NAME" \
  --key '{
    "Performer":{"S":"Megan Thee Stallion"},
    "Performance":{"S":"2025-07-12#20:00#Main Stage"}
  }'

8) Assumptions

Input is CSV (header row) or JSON array of objects; both contain: Performer, Stage, Start, End, Date (ISO YYYY-MM-DD).

Times can be 8:00pm or 20:00; Lambda normalizes to 24h.

No validation of overlapping sets or business rules (per requirements).

Upserts are safe (idempotent) using Performer + Performance as keys.

One SNS email per file processed.

9) Cost & scalability analysis (approximate)

Costs vary by region; below uses rough, commonly-seen on-demand pricing to compare magnitudes.
Assumptions: 1 file/day, all records written once, each record ≈ 0.5 KB (100 lines ≈ 5 KB → given), Lambda 256 MB < 1s.

Daily records	S3 ingest size	DynamoDB writes	SQS requests	Lambda invocations	SNS publishes	Approx daily cost
1,000	~50 KB	1,000 writes	~1	1	1	<$0.05
10,000	~0.5 MB	10,000 writes	~1–2	1–2	1	~$0.05–$0.15
100,000	~5 MB	100,000 writes	few	few	1	~$0.20–$0.60

Notes (order-of-magnitude):

DynamoDB write on-demand ≈ $1.25 per 1M writes ⇒ 100k writes ≈ $0.125.

S3: storage for a few MB is pennies; PUT requests ≈ $0.005 per 1k.

SQS: ≈ $0.40 per 1M requests; we send ~1 message/file.

Lambda: $0.20 per 1M requests + GB-s compute; with 256 MB and <1s, compute is negligible at this scale.

SNS: ≈ $0.50 per 1M publishes; single email/day ≈ $0.0005.

Scalability strategy:

S3→SQS→Lambda buffers spikes; SQS DLQ prevents data loss.

DynamoDB on-demand scales automatically (no capacity planning).

Idempotent upserts handle retries safely.

Use batch writes in Lambda (already implemented).

If record volume grows, split files or stream multiple files; Lambda scales by concurrency.

10) Troubleshooting

PermanentRedirect / S3 region: make sure the deployment bucket and stack are in the same region; create bucket with --create-bucket-configuration LocationConstraint=<region>.

NoSuchBucket on upload: the upload bucket is created by the stack; ensure stack is CREATE_COMPLETE.

S3 → SQS validation: template sets DependsOn: IngestQueuePolicy; if you edited YAML, ensure it’s present.

Pager “frozen” terminal: the AWS CLI may open a pager. Press q to exit or aws configure set cli_pager "".

SNS emails not arriving: confirm the subscription email.

IAM errors: do not hardcode RoleName; use the provided trust/inline policies.

11) Teardown
aws cloudformation delete-stack --stack-name o240-festival
aws cloudformation wait stack-delete-complete --stack-name o240-festival
# Remove the deployment bucket if you created it:
aws s3 rb "s3://$DEP_BUCKET" --force

12) Project structure & key components (for the reviewer)

cloudformation/o240-stack.yaml
Provisions S3 (upload), SQS (+ DLQ), Lambda (Python 3.12), DynamoDB (table + 3 GSIs), SNS, IAM role, and wires S3→SQS→Lambda. Uses DependsOn to avoid S3 notification validation races. Outputs the upload bucket, table, topic.

lambda/handler.py
Pure boto3. Reads SQS payloads, fetches S3 object, auto-detects JSON/CSV, normalizes times, batch-writes to DynamoDB, publishes SNS summary.

sample/
Minimal JSON/CSV to validate end-to-end ingestion.

13) Bonus: Popularity > 80

With GSI PopularityIndex (PopularityBucket, Popularity) you can run:

aws dynamodb query \
  --table-name "$TABLE_NAME" \
  --index-name PopularityIndex \
  --key-condition-expression "PopularityBucket = :b AND Popularity > :min" \
  --expression-attribute-values '{":b":{"S":"POPULARITY"},":min":{"N":"80"}}' \
  --output table

14) Assumptions you can call out in the demo

SQS buffers to ensure resilience vs. spikes; why not S3→Lambda directly? Because SQS gives retries, visibility timeout, DLQ, and smoother scaling.

Single-table DynamoDB intentionally chosen to match access patterns with Queries, not Scans.

Idempotent upserts handle retries from SQS/Lambda safely.

Costs at this scale are dominated by DynamoDB writes; everything else is pennies.

15) Local development notes

Only dependency is boto3 (AWS provides it in the Lambda environment).

If you unit test locally, you can pip install boto3 but do not package extra libs; the handler uses the runtime’s boto3.
