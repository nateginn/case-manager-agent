import sys
sys.path.insert(0, ".")
from tools.gmail_tool import GmailTool

gmail = GmailTool()

# Step 1: fetch real inbox emails and show their API thread IDs
print("=== Recent inbox emails (API thread IDs) ===")
emails = gmail.fetch_inbox_emails(max_results=10)
for e in emails:
    print(f"  email_id={e.get('id')} thread_id={e.get('thread_id')} subject={e.get('subject','')[:60]}")

# Step 2: test fetch_thread on the first email that has a thread_id
threaded = [e for e in emails if e.get("thread_id")]
if not threaded:
    print("\nNo emails with thread_id found.")
    sys.exit(0)

target = threaded[0]
thread_id = target["thread_id"]
email_id = target["id"]
print(f"\n=== Testing fetch_thread on thread_id={thread_id} (excluding email_id={email_id}) ===")

history = gmail.fetch_thread(thread_id, email_id)
print(f"Prior messages in thread: {len(history)}")
for h in history:
    print(f"  sender={h.get('sender')} date={h.get('date')}")

# Step 3: check all emails for any that share a thread_id (reply chains)
print("\n=== Thread ID groups (reply chains) ===")
from collections import Counter
thread_counts = Counter(e.get("thread_id") for e in emails if e.get("thread_id"))
for tid, count in thread_counts.items():
    if count > 1:
        print(f"  thread_id={tid} has {count} messages in inbox fetch")
if all(c == 1 for c in thread_counts.values()):
    print("  (no reply chains found in the top 10 inbox emails)")
