import os
import uuid
from twilio.rest import Client

ACCOUNT_SID = os.environ["TWILIO_ACCOUNT_SID"]
AUTH_TOKEN = os.environ["TWILIO_AUTH_TOKEN"]

AGENT_A_DIAL_TO = os.environ["AGENT_A_DIAL_TO"]   # Twilio number A
AGENT_B_DIAL_TO = os.environ["AGENT_B_DIAL_TO"]   # Twilio number B
AGENT_A_WEBHOOK = os.environ["AGENT_A_WEBHOOK"]   # https://.../twilio_conference_entrypoint
AGENT_B_WEBHOOK = os.environ["AGENT_B_WEBHOOK"]   # https://.../twilio_conference_entrypoint

CONF = os.environ.get("CONF_NAME", f"phase2-{uuid.uuid4().hex[:6]}")
MAX_SECONDS = int(os.getenv("BRIDGE_MAX_SECONDS", "60"))

def main():
    client = Client(ACCOUNT_SID, AUTH_TOKEN)

    url_a = f"{AGENT_A_WEBHOOK}?conf={CONF}"
    url_b = f"{AGENT_B_WEBHOOK}?conf={CONF}"

    # Leg A: B -> A (caller ID is B)
    call_a = client.calls.create(
        to=AGENT_A_DIAL_TO,
        from_=AGENT_B_DIAL_TO,
        url=url_a,
        method="POST",
        time_limit=MAX_SECONDS,
    )

    # Leg B: A -> B (caller ID is A)
    call_b = client.calls.create(
        to=AGENT_B_DIAL_TO,
        from_=AGENT_A_DIAL_TO,
        url=url_b,
        method="POST",
        time_limit=MAX_SECONDS,
    )

    print("Conference:", CONF)
    print("Call A SID:", call_a.sid)
    print("Call B SID:", call_b.sid)

if __name__ == "__main__":
    main()
