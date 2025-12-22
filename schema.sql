CREATE TABLE IF NOT EXISTS transcript_messages (
  id            BIGSERIAL PRIMARY KEY,
  session_id    TEXT NOT NULL,
  call_sid      TEXT,
  stream_sid    TEXT,
  app_instance  TEXT NOT NULL,
  role          TEXT NOT NULL,
  content       TEXT NOT NULL,
  ts_epoch      DOUBLE PRECISION NOT NULL,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_transcript_session
  ON transcript_messages(session_id, ts_epoch);

CREATE INDEX IF NOT EXISTS idx_transcript_call
  ON transcript_messages(call_sid);

