"""Evaluate the configured extraction model with synthetic email only."""
import argparse
import asyncio
import sys
from pathlib import Path
from time import perf_counter
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from personal_os_agent.agents.planner_agent import PlannerAgent

# Case name, synthetic email body, and labeled (task clue, original due wording).
CASES = [
    ("explicit", "Please submit the project report by 2027-10-30T17:00+05:30.",
     [("report", "2027-10-30T17:00+05:30")]),
    ("relative", "Please pay the tuition by Friday at 5 PM.",
     [("tuition", "Friday at 5 PM")]),
    ("missing", "Please submit the invoice. We will announce the exact deadline later.",
     [("invoice", None)]),
    ("appointment", "Our regular meeting takes place at 2027-10-30T17:00+05:30. No preparation is needed.", []),
    ("newsletter", "Read this week's campus newsletter whenever you have spare time.", []),
    ("multiple", "Complete the survey by Monday. Submit the lab report by Thursday at noon.",
     [("survey", "Monday"), ("report", "Thursday at noon")]),
    ("revised", "The earlier Tuesday deadline for your application has been canceled. Submit the application by Friday.",
     [("application", "Friday")]),
    ("completed", "We received your assignment before the deadline. Everything is complete.", []),
]


async def evaluate(cases: list) -> int:
    planner = PlannerAgent()
    client = planner._llm.with_options(timeout=25, max_retries=0)
    input_tokens = output_tokens = 0

    async def complete(**kwargs):
        nonlocal input_tokens, output_tokens
        result = await client.chat.completions.create(**kwargs)
        if result.usage:
            input_tokens += result.usage.prompt_tokens or 0
            output_tokens += result.usage.completion_tokens or 0
        return result

    planner._llm = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=complete)))
    matched = actual = expected = date_correct = completed = 0
    started = perf_counter()
    try:
        for case, message, labels in cases:
            event = {
                "type": "email", "synthetic": True, "summary": "Synthetic evaluation email",
                "payload": {
                    "subject": "Synthetic evaluation email",
                    "from": "sender@example.com",
                    "snippet": "Sample email preview",
                    "body": message,
                },
            }
            try:
                tasks = await planner._plan_event(event)
            except Exception as exc:
                status = getattr(exc, "status_code", None)
                print(f"{case}: model request failed ({type(exc).__name__}, HTTP {status or 'unavailable'})")
                if status in (401, 402, 403, 404, 429):
                    break
                continue

            completed += 1
            expected += len(labels)
            actual += len(tasks)
            remaining = tasks[:]
            case_matches = case_dates = 0
            for clue, due in labels:
                task = next((item for item in remaining if clue in item["deadline"].casefold()), None)
                if task is None:
                    continue
                remaining.remove(task)
                case_matches += 1
                wording = task["due"].strip()
                correct = due.casefold() in wording.casefold() if due else not wording
                case_dates += int(correct)
            matched += case_matches
            date_correct += case_dates
            print(f"{case}: {case_matches}/{len(labels)} expected tasks matched, "
                  f"{case_dates}/{case_matches} due fields correct, {len(remaining)} extra tasks")
    finally:
        await client.close()

    print(f"Completed {completed}/{len(cases)} cases in {perf_counter() - started:.1f}s.")
    if completed:
        print(f"Task precision {matched}/{actual or 0}; recall {matched}/{expected or 0}; "
              f"due wording {date_correct}/{matched or 0}.")
        print(f"Model reported {input_tokens} input and {output_tokens} output tokens.")
    return 0 if completed == len(cases) else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case", choices=[case[0] for case in CASES])
    args = parser.parse_args()
    raise SystemExit(asyncio.run(evaluate([case for case in CASES if not args.case or case[0] == args.case])))
