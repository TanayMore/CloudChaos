# Cloud Chaos Lab

Minimal Flask app that demonstrates basic chaos engineering on a single EC2 instance:
- Launch an EC2 instance
- Trigger chaos (randomly stop or reboot)
- Monitor CloudWatch metrics (CPU + status checks)
- Send alerts via SNS
- Automatically start the instance again if it gets stopped

## Run locally

```powershell
cd "C:\Users\Tanay\OneDrive\Documents\cloud-chaos-lab"
pip install -r requirements.txt
python app.py
```

Open: `http://localhost:5000/`

## Required environment variables

AWS credentials (any standard method works, including IAM role on EC2):
- `AWS_ACCESS_KEY_ID`
- `AWS_SECRET_ACCESS_KEY`
- `AWS_REGION` (example: `us-east-1`)

SNS alerting:
- `ALERT_EMAIL` (used for the email subscription)
- `SNS_TOPIC_NAME` (optional; default `cloud-chaos-lab-alerts`)

Optional configuration:
- `TARGET_INSTANCE_ID` (optional; otherwise you can launch an instance from the UI)
- `AUTO_RECOVERY_ENABLED` (optional; default `true`)
- `HIGH_CPU_THRESHOLD` (optional; default `70`)

Launch form defaults (optional):
- `DEFAULT_AMI_ID`
- `DEFAULT_INSTANCE_TYPE` (optional; default `t3.micro`)
- `DEFAULT_KEY_NAME`
- `DEFAULT_SECURITY_GROUP_IDS` (comma-separated)
- `DEFAULT_SUBNET_ID`

## Notes for SNS

After you click **Setup SNS**, you’ll usually need to confirm the subscription via the email link.

