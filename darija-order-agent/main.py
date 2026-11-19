"""CLI helper (the website is the normal way to start calls).

  python main.py init-db
  python main.py seed-demo +2135XXXXXXXX
  python main.py call DZ-DEMO-001
"""
import argparse
from dotenv import load_dotenv

load_dotenv()
from db import init_db, create_demo_order
from telephony import start_call

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Algerian Darija order-confirmation caller")
    parser.add_argument("command", choices=["init-db", "seed-demo", "call"])
    parser.add_argument("value", nargs="?", default="DZ-DEMO-001",
                        help="Order ID for 'call', or phone number (E.164) for 'seed-demo'")
    args = parser.parse_args()
    init_db()
    if args.command == "init-db":
        print("Database initialized.")
    elif args.command == "seed-demo":
        if not args.value.startswith("+"):
            raise SystemExit("Give your phone in E.164 format, e.g. python main.py seed-demo +2135XXXXXXXX")
        create_demo_order(args.value)
        print("Demo order DZ-DEMO-001 created.")
    elif args.command == "call":
        print("Call started. SID:", start_call(args.value))
