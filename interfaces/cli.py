"""interfaces/cli.py — Command-line interface for the chatbot."""
from __future__ import annotations
import logging
import uuid

log = logging.getLogger("cli")


def run(core, mongo, redis, cap_names: list[str], session_id: str | None = None):
    """Run the chatbot in CLI mode."""
    from session import SessionManager

    if session_id:
        session = SessionManager(session_id, mongo, redis, cap_names)
        print(f"Resumed session: {session_id} ({session.turn_count} prior turns)")
    else:
        session = SessionManager(str(uuid.uuid4()), mongo, redis, cap_names)
        print(f"New session: {session.session_id}")

    print(f"Capabilities: {', '.join(cap_names)}")
    print("Commands: 'reset' | 'recent' | 'caps' | 'quit'")
    print("-" * 60)

    while True:
        try:
            question = input("\n> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nGoodbye!")
            break

        if not question:
            continue

        # Built-in commands
        if question.lower() in ("quit", "exit", "q"):
            print("Goodbye!")
            break

        if question.lower() == "reset":
            session.clear()
            session = SessionManager(str(uuid.uuid4()), mongo, redis, cap_names)
            print(f"New session: {session.session_id}")
            continue

        if question.lower() == "recent":
            msgs = session.get_recent()
            if not msgs:
                print("  (no recent messages in Redis)")
            for m in msgs:
                prefix = "You" if m["role"] == "user" else "Bot"
                print(f"  [{prefix}] {m['content'][:120]}")
            continue

        if question.lower() == "caps":
            for cap in core._caps:
                ok, msg = cap.startup_check()
                print(f"  {'✓' if ok else '✗'} {cap.name}: {cap.description}")
            continue

        # Ask the chatbot
        try:
            result = core.ask(question, session)
        except Exception as e:
            print(f"⚠️  Error: {e}")
            log.error("ask() failed", exc_info=True)
            continue

        # Print tool events
        for event in result.tool_events:
            status = "✓" if not event.error else f"✗ {event.error}"
            print(f"  🔧 {event.tool_name} {status}")

        # Print answer
        print(f"\n{result.answer}")

        # Print token usage
        print(f"\n  ─ {result.input_tokens} input "
              f"({result.cached_tokens} cached ↓) | "
              f"{result.output_tokens} output tokens")
