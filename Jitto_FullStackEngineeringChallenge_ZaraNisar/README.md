O240 Music Festival: use cloudformation/o240-stack.yaml to deploy.
Package lambda like:
  cd lambda && zip ../function.zip handler.py && cd ..
Then create two buckets (deployment + upload), copy function.zip to the deployment bucket, and run create-stack.
