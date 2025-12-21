# orchestrator.py
import os
from twilio.rest import Client

ACCOUNT_SID = os.environ["TWILIO_ACCOUNT_SID"]
AUTH_TOKEN  = os.environ["TWILIO_AUTH_TOKEN"]

AGENT_A_NUMBER = os.environ["AGENT_A_DIAL_TO"]
AGENT_B_NUMBER = os.environ["AGENT_B_DIAL_TO"]

MAX_SECONDS = int(os.getenv("BRIDGE_MAX_SECONDS", "60"))

AGENT_B_WEBHOOK = os.environ["AGENT_B_WEBHOOK"] 

def main():
    client = Client(ACCOUNT_SID, AUTH_TOKEN)

    call = client.calls.create(
        to=AGENT_B_NUMBER,
        from_=AGENT_A_NUMBER,
        url=AGENT_B_WEBHOOK,         
        method="POST",
        time_limit=MAX_SECONDS,
    )

    print("Call SID:", call.sid)

if __name__ == "__main__":
    main()