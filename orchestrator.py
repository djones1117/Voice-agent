# orchestrator.py
import os
from twilio.rest import Client

ACCOUNT_SID = os.environ["TWILIO_ACCOUNT_SID"]
AUTH_TOKEN = os.environ["TWILIO_AUTH_TOKEN"]

AGENT_A_NUMBER = os.environ["AGENT_A_DIAL_TO"]       # Twilio number A
AGENT_B_NUMBER = os.environ["AGENT_B_DIAL_TO"]       # Twilio number B

MAX_SECONDS = int(os.getenv("BRIDGE_MAX_SECONDS", "60"))

AGENT_A_WEBHOOK_PHASE2 = os.environ["AGENT_A_WEBHOOK_PHASE2"]


def main():
    client = Client(ACCOUNT_SID, AUTH_TOKEN)

    call = client.calls.create(
        to=AGENT_B_NUMBER,
        from_=AGENT_A_NUMBER,
        url=AGENT_A_WEBHOOK_PHASE2,
        method="POST",
        time_limit=MAX_SECONDS,
    )

    print("Call SID:", call.sid)

if __name__ == "__main__":
    main()
