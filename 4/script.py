#!/usr/bin/env python3
import subprocess
import json
import re
import sys
import mysql.connector
from mysql.connector import errorcode

# ——— Database configuration — update to your settings ———
DB_CONFIG = {
    "host": "localhost",
    "user": "root",             # ← your MySQL user
    "password": "", # ← your MySQL password
    "database": "customer_support"
}

def get_db_connection():
    try:
        return mysql.connector.connect(**DB_CONFIG)
    except mysql.connector.Error as e:
        if e.errno == errorcode.ER_ACCESS_DENIED_ERROR:
            print("Error: Authentication to MySQL failed.", file=sys.stderr)
        elif e.errno == errorcode.ER_BAD_DB_ERROR:
            print("Error: Specified database does not exist.", file=sys.stderr)
        else:
            print(f"Error: MySQL connection failure: {e}", file=sys.stderr)
        sys.exit(1)

def setup_table(cursor):
    """
    Create or migrate the user_info table with:
      id, name, email, account_number,
      query_type, other_details, answered
    """
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS user_info (
            id              INT AUTO_INCREMENT PRIMARY KEY,
            name            VARCHAR(255),
            email           VARCHAR(255),
            account_number  VARCHAR(255),
            query_type      VARCHAR(50),
            other_details   TEXT,
            answered        TINYINT(1)
        )
    """)
    # Add missing columns if we’re upgrading an old schema
    for col, definition in [
        ("query_type",    "VARCHAR(50)"),
        ("other_details", "TEXT"),
        ("answered",      "TINYINT(1)")
    ]:
        try:
            cursor.execute(f"ALTER TABLE user_info ADD COLUMN {col} {definition}")
        except mysql.connector.Error as e:
            # 1060 = ER_DUP_FIELDNAME (column already exists)
            if e.errno != errorcode.ER_DUP_FIELDNAME:
                raise

def ollama_generate(prompt: str, model="llama2-uncensored:latest") -> str:
    """
    Calls `ollama run <model> [prompt]`. Returns stdout or "" on error.
    """
    cmd = ["ollama", "run", model]
    if prompt:
        cmd.append(prompt)
    try:
        proc = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=60
        )
        if proc.returncode != 0:
            print(
                f"Error: Ollama CLI exited with code {proc.returncode}: "
                f"{proc.stderr.strip()}",
                file=sys.stderr
            )
            return ""
        return proc.stdout.strip()
    except subprocess.TimeoutExpired:
        print("Error: Ollama CLI timed out.", file=sys.stderr)
        return ""
    except Exception as e:
        print(f"Error: Ollama CLI invocation failed: {e}", file=sys.stderr)
        return ""

def extract_user_info(user_input: str, ai_response: str) -> dict:
    """
    Ask the model to output ONLY this valid JSON object with exactly these keys:
      name           – the *user’s* full name (string or "")
      email          – user's email (string or "")
      account_number – user's account number (string or "")
      query_type     – one of: balance, transfer, loan, investment, other
      other_details  – any extra text (string or "")
      answered       – true if the AI reply fully handled the request

    No markdown fences, no comments, no extra fields, no trailing commas.
    """
    prompt = f"""
You are a secure financial assistant. Extract from the *user’s message* and *your reply*
exactly the following fields in one valid JSON object (no markdown, no comments,
no extra keys, no trailing commas, braces balanced):

  name           – the user’s full name or ""
  email          – the user’s email address or ""
  account_number – the user’s account number or ""
  query_type     – one of: balance, transfer, loan, investment, other
  other_details  – any additional details or ""
  answered       – true if your reply addressed the request, else false

User message:
\"\"\"{user_input}\"\"\"

Your reply:
\"\"\"{ai_response}\"\"\"
"""
    raw = ollama_generate(prompt)
    if not raw:
        return {
            "name": "",
            "email": "",
            "account_number": "",
            "query_type": "other",
            "other_details": user_input,
            "answered": False
        }

    # 1) Strip markdown fences
    raw = re.sub(r"```.*?```", "", raw, flags=re.DOTALL).strip()
    # 2) Extract the first {...} block
    start = raw.find("{")
    end   = raw.rfind("}")
    blob  = raw[start:end+1] if start != -1 and end != -1 else raw
    # 3) Remove any trailing commas before } or ]
    blob = re.sub(r",\s*(?=[}\]])", "", blob)
    # 4) Auto-repair unbalanced braces
    opens  = blob.count("{")
    closes = blob.count("}")
    if closes < opens:
        blob += "}" * (opens - closes)

    try:
        data = json.loads(blob)
    except json.JSONDecodeError:
        print(f"Error: JSON parsing failed. Blob was:\n{blob}", file=sys.stderr)
        return {
            "name": "",
            "email": "",
            "account_number": "",
            "query_type": "other",
            "other_details": user_input,
            "answered": False
        }

    # Enforce allowed query_type
    allowed = {"balance", "transfer", "loan", "investment", "other"}
    qt = str(data.get("query_type", "")).lower()
    data["query_type"] = qt if qt in allowed else "other"

    # Fill missing keys safely
    data.setdefault("name", "")
    data.setdefault("email", "")
    data.setdefault("account_number", "")
    data.setdefault("other_details", "")
    data.setdefault("answered", False)

    return data

def main():
    conn   = get_db_connection()
    cursor = conn.cursor()
    setup_table(cursor)
    conn.commit()

    print("Customer Support AI (type 'exit' or 'quit' to terminate)\n")

    while True:
        user_input = input("User: ").strip()
        if user_input.lower() in ("exit", "quit"):
            break

        # 1) Get the assistant’s reply
        ai_response = ollama_generate(user_input)
        print("\nAI Response:\n", ai_response, "\n")

        # 2) Extract structured data + answered flag
        info = extract_user_info(user_input, ai_response)

        # 3) Persist to MySQL
        try:
            cursor.execute(
                """
                INSERT INTO user_info
                  (name, email, account_number,
                   query_type, other_details, answered)
                VALUES (%s, %s, %s, %s, %s, %s)
                """,
                (
                    info["name"],
                    info["email"],
                    info["account_number"],
                    info["query_type"],
                    info["other_details"],
                    int(info["answered"])
                )
            )
            conn.commit()
            print("User information recorded successfully.\n")
        except mysql.connector.Error as e:
            print(f"Error: Failed to insert user info: {e}", file=sys.stderr)

    cursor.close()
    conn.close()

if __name__ == "__main__":
    main()