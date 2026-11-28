"""A synthetic, local plugin. It emits only structured example data."""
import json
import sys


def main():
    command = sys.argv[1]
    if command == "status":
        print(json.dumps({"schema": "bitcadence.status/1", "health": "ok", "metrics": {"count": 1}}))
    elif command == "tick" or (command == "greet" and len(sys.argv) == 3):
        # Greet receives one literal name argument. The example does not print
        # user parameters or perform any external side effects.
        print("@@bcd " + json.dumps({"type": command, "state": "ok"}))
    else:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
