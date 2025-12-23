# Novasonic (AWS Bedrock) Bidirectional Streaming Voice Agent — FastAPI + Twilio

Real-time speech-to-speech voice agent using **Amazon Nova Sonic (Bedrock Runtime bidirectional streaming)** + **Twilio Voice Media Streams**, with **Postgres** transcript logging and **configurable system prompts**.  
Designed to run as **two independently hosted app instances** (Agent A / Agent B) by running an instance of the source code and exporting env values. (Change prompts, voices, etc. Change the VOICE_NAME if you want a different voice - matthew works. Tiffany is default)

---

## Tech stack

- **Python 3.13**
- **FastAPI** + **Uvicorn**
- **AWS Bedrock Runtime bidirectional streaming** (`aws_sdk_bedrock_runtime`)
- **Twilio Voice** + **Media Streams (WebSocket)**
- **Audio pipeline:** Twilio μ-law 8kHz ⇄ PCM16 (Nova input 16kHz / output 24kHz) using `numpy`, `scipy`, `g711`
- **Postgres** for transcript storage (`asyncpg`)
- **ngrok** for public HTTPS/WSS during local dev - using AWS native domain system would be ideal for deployment

---

## Repo layout (expected)

- `app.py` — FastAPI app, Twilio webhook + Media Streams WS, Nova Sonic client, transcript logging
- `db_utils.py` — asyncpg pool + queries
- `schema.sql` — `transcript_messages` table
- `env.sh` — local environment exports (you maintain per instance)
- `requirements.txt`
- `templates/index.html` — optional test page

---

## Prerequisites

### 1 AWS / Bedrock
- AWS credentials available as environment variables 
- Bedrock access enabled for your account/region.
- Model ID (default in code): `amazon.nova-2-sonic-v1:0`

### 2 Twilio
You need **two Twilio phone numbers** if you want Agent A and Agent B to be independently callable.
- Each number should be configured to hit that instance’s `/twilio_entrypoint` webhook URL (public HTTPS).

### 3 Local tools
- Python 3.13
- Docker (for local Postgres)
- ngrok - two domains to tunnel

---

## Install & local setup

### 1 Create venv + install deps
```bash
python3.13 -m venv env
source env/bin/activate
pip install -r requirements.txt
```
## 2 Start Postgres (Docker)

If you don’t already have one running:

```bash
docker run --name local-postgres \
  -e POSTGRES_USER=voice_app \
  -e POSTGRES_PASSWORD=voice_pass \
  -e POSTGRES_DB=voice_agent_app \
  -p 5432:5432 \
  -d postgres:16
  ```
 Your `DATABASE_URL` should look like:

~~~bash
export DATABASE_URL="postgresql://voice_app:voice_pass@localhost:5432/voice_agent_app"
~~~

The app loads `schema.sql` on startup (lifespan) and will create the table if missing.

## Environment variables (per app instance)

Create and configure `env.sh` in each the folder and `source` it before running.

### Core

~~~bash
export AWS_REGION="us-east-1"
export AWS_ACCESS_KEY_ID=
export AWS_SECRET_ACCESS_KEY

export DATABASE_URL="postgresql://voice_app:voice_pass@localhost:5432/voice_agent_app"


export AGENT_NAME="A"

### Twilio


export TWILIO_ACCOUNT_SID="ACxxxxxxxx"
export TWILIO_AUTH_TOKEN="xxxxxxxx"
export TWILIO_FROM_NUMBER="+1xxxxxxxxxx"
~~~

### Public URLs (ngrok)
~~~bash
# Public HTTPS base for Twilio webhook to reach /twilio_entrypoint - The two instances should point at two different urls!!!
export PUBLIC_BASE_URL="https://<YOUR_NGROK_DOMAIN>"

# Public WSS base for Twilio Media Streams to reach /voice_agent
export WSS_VOICE_URL="wss://<YOUR_NGROK_DOMAIN>"
~~~




# Local server port for this instance make - sure ngrok is also pointed at the same port. if running two agents - point the second instance at 8081 and the other ngrok at 8081. 
~~~bash
export PORT="8080"
~~~

### Prompt + voice
These can be exported through your terminal in each instance to play around with personalities and scenarios
~~~bash they do not need to be stored in your env.sh file
export AGENT_VOICE="tiffany"
export AGENT_SYSTEM_PROMPT='You are a helpful, friendly voice assistant named Sally. Speak clearly, keep responses brief, and be polite.'
~~~




# Running ONE agent locally (Phase 1)

## Terminal 1 — run the API

~~~bash
source env/bin/activate
source env.sh
python3 app.py - should initally be on port 8080
~~~

## Terminal 2 — start ngrok on the SAME port 

~~~bash
ngrok http $PORT - you can set it to 8080 or 8081 or whatever port app.py is running on
~~~

Copy the ngrok **HTTPS** forwarding URL to use a simple UI test:

~~~bash
export PUBLIC_BASE_URL="https://…"
export WSS_VOICE_URL="wss://…"  # same domain, just wss://
~~~


If you change env.sh then -
Restart uvicorn after updating env:


# Ctrl+C to stop then re-run app.py after sourcing your updated env


---

## Configure Twilio webhook for inbound calls

In **Twilio Console → Phone Number → Voice**:

- **A CALL COMES IN** → **Webhook** → **POST**
- URL:

~~~text
${PUBLIC_BASE_URL}/twilio_entrypoint
~~~

---

## Make a call (Phase 1)

Call the Twilio number. Twilio will:

- hit `/twilio_entrypoint` (returns TwiML with `<Connect><Stream/>`)
- open a WebSocket to `${WSS_VOICE_URL}/voice_agent`
- stream audio to your agent and receive synthesized audio back

---

# Running TWO independent agents locally (Phase 2)

Phase 2 runs two independent instances (A and B) from the same codebase by starting the service twice on different ports and exporting different env values per terminal



## 2 Change ONLY the per-instance env values in the second instance

### Agent B (instance B)

> Do **not** reuse Agent A’s `PORT`, `PUBLIC_BASE_URL`, `WSS_VOICE_URL`, or `TWILIO_FROM_NUMBER`.
> Agent B must have its **own** port + ngrok tunnel + Twilio number.

#do this first
```
source env.sh
```
~~~bash
# Required per-instance values
export PORT="8081"
export AGENT_NAME="B"

# Twilio number used by *this* instance when making outbound calls. must be different from a
export TWILIO_FROM_NUMBER="+1XXXXXXXXXX"   # <-- Twilio Number B

# Public URLs for *this* instance (ngrok-B domain). MUST be different than Agent A or app will not work. 
export PUBLIC_BASE_URL="https://<ngrok-b-domain>.ngrok-free.dev"
export WSS_VOICE_URL="wss://<ngrok-b-domain>.ngrok-free.dev"

# Optional scenario prompt + voice overrides (override defaults in app.py via os.getenv) important if you want them to have diff voices/personalities/scenarios
export AGENT_VOICE="matthew"
export AGENT_SYSTEM_PROMPT='You are Matthew. Your favorite lord of the rings character is Gandalf. You are answering questions about lord of the rings'
~~~

# Optional scenario prompt:
# export AGENT_SYSTEM_PROMPT="..." enter whatever you would like - just make sure it makes sense or the agents convo might be chaotic.


### 3 Start both apps + both ngroks




### Agent A

~~~bash

source env/bin/activate
source env.sh
uvicorn app:api --host 0.0.0.0 --port 8080 # or python3 app.py
~~~

~~~bash
ngrok http 8080
~~~

### Agent B

~~~bash 
source env/bin/activate #if not activated - we dont source the env since we already did it before exporting new values
uvicorn app:api --host 0.0.0.0 --port 8081 # or python3 app.py
~~~

~~~bash
ngrok http 8081
~~~
~~~bash
# Make sure your env vars are actually set for this instance: or it will not work
env | egrep '^(PORT|AGENT_VOICE|AGENT_SYSTEM_PROMPT|TWILIO_FROM_NUMBER|PUBLIC_BASE_URL|WSS_VOICE_URL)='

# If you have a reserved ngrok domain and want to force it:
ngrok http 8081 --url=https://<ngrok-b-domain>.ngrok-free.dev
~~~

---

## 4 Twilio config (two numbers)

- **Number A** inbound webhook → `https://<ngrok-A>/twilio_entrypoint` (POST)
- **Number B** inbound webhook → `https://<ngrok-B>/twilio_entrypoint` (POST)

---

## 5 Kicking off calls (outbound) - optional go to the forward address in your ngrok tunnel and you can test calls from there - you can get the agent to call you or you can call the agent. The agents can also call each other. copy and paste your ngrok url in a browser for easy testing

Your app exposes:

- `POST /start_outbound_call` with JSON: `{"to":"+1<insert number>"}` must be in +1xxxxxxxxxx format

Example:

~~~bash
curl -s -X POST "http://localhost:8080/start_outbound_call" \
  -H "Content-Type: application/json" \
  -d '{"to":"+1XXXXXXXXXX"}'
~~~



# Prompt configuration (simulated scenarios)

Prompts are pure env config:

- `AGENT_SYSTEM_PROMPT` controls personality/behavior - you can export new prompts from the command line or go to the top of app.py where its defined and manually change it. 
- Restart uvicorn after changing prompts. Save the app and run again to test. 

Example prompts:

### Agent A (prompt)

~~~bash
export AGENT_SYSTEM_PROMPT="You are a helpful, friendly voice assistant named Sally.
    You like Lord of the rings. Respond to matthew's responses that are about Lord of the rings.
    Speak clearly, keep responses brief, and be polite.",
~~~

### Agent B (candidate)

~~~bash
export AGENT_SYSTEM_PROMPT="You are Matthew. Your responses are short. Your favorite lord of the rings character is Gandalf. You are having a back and forth conversation with sally about lord of the rings. Keep responses short and clear"
~~~

---

# Transcript logging (Postgres)

## Schema

Rows are stored in:

~~~text
transcript_messages(
  session_id,
  call_sid,
  stream_sid,
  app_instance,
  role,
  content,
  ts_epoch,
  created_at
)
~~~

Notes:



`role` values:

- `system_prompt`
- `user`
- `agent` 

## Transcript queries (Postgres)

These queries help you verify that transcripts are being stored correctly and support **reporting + retrieval** (timestamps, session identifiers, speaker/role labels, and message content).

### 1 Quick query (run from your normal shell, NOT inside psql)

Shows: latest transcript rows across all sessions (useful for fast sanity checks).

```bash
docker exec -it local-postgres psql -U voice_app -d voice_agent_app -c \
"SELECT
   id,
   to_char(to_timestamp(ts_epoch), 'YYYY-MM-DD HH24:MI:SS') AS ts,
   session_id,
   role,
   call_sid,
   stream_sid,
   left(content, 80) AS content
 FROM transcript_messages
 ORDER BY ts_epoch DESC
 LIMIT 25;"
```
### 2 Inside psql — session rollup (reporting + retrieval)

Shows: per-session summary (start time, last activity time, message count). This makes it easy to find the most recent session to inspect.

```bash
SELECT
  session_id,
  MIN(to_timestamp(ts_epoch)) AS started_at,
  MAX(to_timestamp(ts_epoch)) AS last_at,
  COUNT(*)                    AS message_count
FROM transcript_messages
GROUP BY session_id
ORDER BY last_at DESC
LIMIT 20;
```

### Inside psql — latest transcript messages (timestamp + role + content)

Shows: most recent messages across all sessions (easy “did logging work?” sanity check).

```bash
SELECT
  to_char(to_timestamp(ts_epoch), 'YYYY-MM-DD HH24:MI:SS') AS ts,
  session_id,
  role,
  left(content, 200) AS content
FROM transcript_messages
ORDER BY ts_epoch DESC
LIMIT 50;
```
---

# Local “health checks” (sanity)

Home page:

```bash
GET http://localhost:<PORT>/


List sessions:


GET http://localhost:<PORT>/sessions


Fetch a session:


GET http://localhost:<PORT>/sessions/<session_id>
```
---

## AWS CDK deployment (planned implementation)

 I focused on delivering the working local + Twilio + bidirectional streaming system first. If I had additional time, I would deploy the same architecture to AWS with CDK so the entire stack can be created and destroyed reliably (`cdk deploy` / `cdk destroy`).

### Target architecture (AWS)

**Compute**
- **ECS Fargate (recommended)**: run **two independent services** (Agent A + Agent B) from the same container image, each with its own environment variables (`AGENT_NAME`, `PORT`, prompt, Twilio number, etc.).
  - Alternative: **ECS service + auto-scaling** if expecting higher call volume.
  - I would *not* use Lambda for the Media Streams WebSocket server because long-lived WebSockets + real-time audio streaming are not a great fit for Lambda’s execution model.

**Networking**
- **VPC** with public subnets (for ALB) and private subnets (for ECS tasks + RDS).
- **Application Load Balancer (ALB)** with:
  - HTTPS listener (ACM certificate)
  - Path-based routing:
    - `/a/*` → Agent A target group
    - `/b/*` → Agent B target group
  - **WebSocket support** via ALB for `/voice_agent` (ALB supports WebSockets over HTTP/1.1).
- **Route 53** domain (e.g., `voice.example.com`) for stable public URLs.
- **Security groups**:
  - ALB SG: allow inbound 443 from internet
  - ECS SG: allow inbound only from ALB SG on container port
  - RDS SG: allow inbound only from ECS SG on 5432

**Database**
- **Amazon RDS Postgres** (or Aurora Postgres) in private subnets.
- Credentials stored in **AWS Secrets Manager**.
- `DATABASE_URL` assembled at runtime from the secret + RDS endpoint.

**Secrets & config**
- **AWS Secrets Manager** for:
  - Twilio creds: `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`
  - DB credentials
- **SSM Parameter Store** (or Secrets Manager) for non-secret config:
  - `BEDROCK_MODEL_ID`, `AWS_REGION`, `AGENT_VOICE`, `AGENT_SYSTEM_PROMPT`
- Per-service env vars set via CDK:
  - Agent A: `AGENT_NAME=A`, Twilio Number A, prompt A
  - Agent B: `AGENT_NAME=B`, Twilio Number B, prompt B

**IAM**
- ECS Task Role policy:
  - Bedrock streaming invoke:
    - `bedrock:InvokeModel`
    - `bedrock:InvokeModelWithResponseStream`
    - (and/or whichever action is required for the bidirectional streaming API in the SDK used)
  - Read secrets:
    - `secretsmanager:GetSecretValue` for Twilio + DB secret
  - Optional: CloudWatch logs permissions (usually via execution role)
- ECS Execution Role:
  - pull container image from ECR
  - write logs to CloudWatch

**Logging/observability**
- CloudWatch Log Groups per service (Agent A / Agent B)
- Optional (future): X-Ray tracing, structured JSON logging, metrics on active calls

---

### Public endpoints (what Twilio would call in AWS)

After deploy, I’d have a stable domain, for example:

- Agent A Twilio webhook: `https://voice.example.com/a/twilio_entrypoint`
- Agent A Media Streams WS: `wss://voice.example.com/a/voice_agent`
- Agent B Twilio webhook: `https://voice.example.com/b/twilio_entrypoint`
- Agent B Media Streams WS: `wss://voice.example.com/b/voice_agent`

In the FastAPI app, I would support a configurable `BASE_PATH` (or mount routers) so each service can serve under `/a` or `/b` cleanly when behind the ALB.

---

### CDK stack outline (what I would implement )

**1 Container build + registry**
- Dockerize the app.
- CDK creates an **ECR repo** and the deploy process pushes the image.

**2 Network**
- `Vpc` with 2–3 AZs
- `ApplicationLoadBalancer` + HTTPS listener + ACM cert
- Target groups for Agent A and Agent B (path routing)

**3 Compute**
- `Cluster` + two `FargateService`s:
  - `voice-agent-a-service`
  - `voice-agent-b-service`
- Each service:
  - points to the same container image
  - sets env vars specific to that agent
  - attaches to its own target group behind the ALB

**4 Database**
- `DatabaseInstance` (Postgres) in private subnets
- DB credentials generated/stored in Secrets Manager
- ECS services granted permission to read DB secret

**5 IAM**
- Task role with Bedrock invoke permissions + Secrets Manager read
- Execution role for ECR pull + CloudWatch logs

---

### How to deploy / destroy (commands)

Assuming CDK is set up in a `cdk/` folder:

```bash
# one-time
npm install -g aws-cdk
cd cdk
python3.13 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt  # if CDK is in python
cdk bootstrap

# deploy
cdk synth
cdk deploy

# destroy
cdk destroy

