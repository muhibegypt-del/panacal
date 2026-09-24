"""Child-process fixture: reproduces Argyll pipe behaviour without a meter."""
import os
import sys
import time

scenario = sys.argv[1]
assert os.environ.get("ARGYLL_NOT_INTERACTIVE") == "1"
PROMPT = "Hit ESC or Q to exit, any other key to take a reading: "

def prompt():
    # Flush split fragments with no trailing newline, as real Argyll does.
    for fragment in (PROMPT[:19], PROMPT[19:48], PROMPT[48:]):
        sys.stdout.write(fragment)
        sys.stdout.flush()

if scenario == "startup_hang":
    time.sleep(60)  # Parent must reap even a child that ignores 'q'.
elif scenario == "startup_exit":
    sys.stderr.write("USB port unavailable: fixture diagnostic")
    sys.stderr.flush()
    sys.exit(3)

prompt()
number = 0
for command in sys.stdin:
    if command == "q\n":
        if scenario == "confirm_quit":
            sys.stdout.write("Hit Esc or Q to give up, any other key to retry: ")
            sys.stdout.flush()
            assert sys.stdin.readline() == "q\n"
        break
    assert command == " \n", repr(command)
    number += 1
    if scenario == "read_hang":
        time.sleep(60)
    if scenario == "no_result":
        prompt()
        continue
    if scenario == "refresh_failure":
        print("\nMeasuring refresh rate failed", flush=True)
        prompt()
        continue
    if scenario == "nonfinite":
        print("\nResult is XYZ: 1e999 2 3", flush=True)
    else:
        print(f"\nResult is XYZ: {number} {number+1} {number+2}, D50 Lab: 1 2 3", flush=True)
    if scenario == "exit_after_result":
        sys.exit(4)
    # A result is not a ready indication; triggers must wait for this prompt.
    time.sleep(0.04)
    print("Measurement finished; returning to ready state.", flush=True)
    prompt()
