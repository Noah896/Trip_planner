#!/usr/bin/env python
"""批量评测旅行规划质量。

默认只处理已有响应,不调用模型。真实调用必须显式传 ``--yes-live``。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from auth_client import get_auth_headers  # noqa: E402
from evals.evaluator import (  # noqa: E402
    build_report,
    evaluate_case,
    load_golden_cases,
    render_markdown,
)


DEFAULT_CASES = ROOT / "evals" / "golden_cases.json"
DEFAULT_OUTPUT = ROOT / "evals" / "results"


def _select_cases(
    cases: List[Dict[str, Any]],
    *,
    case_ids: Optional[List[str]],
    limit: Optional[int],
) -> List[Dict[str, Any]]:
    selected = cases
    if case_ids:
        wanted = set(case_ids)
        selected = [case for case in selected if case["id"] in wanted]
        missing = wanted - {case["id"] for case in selected}
        if missing:
            raise ValueError(f"未知 case: {', '.join(sorted(missing))}")
    if limit is not None:
        selected = selected[: max(0, int(limit))]
    return selected


def _load_replay(path: Path) -> Dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    responses = payload.get("responses")
    if not isinstance(responses, dict):
        raise ValueError("replay 文件必须包含 responses 对象")
    return responses


async def _run_live_case(
    client: httpx.AsyncClient,
    backend_url: str,
    case: Dict[str, Any],
) -> Dict[str, Any]:
    started = time.monotonic()
    result: Optional[Dict[str, Any]] = None
    error_message = ""
    async with client.stream(
        "POST",
        f"{backend_url}/api/trip/plan/stream",
        json=case["request"],
    ) as response:
        if response.status_code != 200:
            body = (await response.aread()).decode("utf-8", errors="replace")
            return {
                "success": False,
                "degraded": False,
                "message": f"HTTP {response.status_code}: {body[:300]}",
                "elapsed": round(time.monotonic() - started, 2),
                "data": None,
            }

        async for line in response.aiter_lines():
            if not line.startswith("data:"):
                continue
            event = json.loads(line[len("data:") :].strip())
            if event.get("type") == "result":
                result = event
            elif event.get("type") == "error":
                error_message = str(event.get("message") or "SSE 返回 error")

    wall_time = round(time.monotonic() - started, 2)
    if result is not None:
        return {
            **result,
            "elapsed": result.get("elapsed", wall_time),
        }
    return {
        "success": False,
        "degraded": False,
        "message": error_message or "流结束但没有 result 事件",
        "elapsed": wall_time,
        "data": None,
    }


async def _live_results(
    cases: List[Dict[str, Any]],
    backend_url: str,
) -> List[Dict[str, Any]]:
    results = []
    async with httpx.AsyncClient(timeout=330.0) as client:
        client.headers.update(await get_auth_headers(client, backend_url))
        for index, case in enumerate(cases, start=1):
            print(
                f"[{index}/{len(cases)}] live {case['id']} "
                f"({case['request']['city']} {case['request']['travel_days']}天)",
                flush=True,
            )
            response = await _run_live_case(client, backend_url, case)
            evaluation = evaluate_case(case, response)
            results.append(evaluation)
            failed = ", ".join(evaluation["failed_checks"]) or "-"
            print(
                f"  {'PASS' if evaluation['passed'] else 'FAIL'} "
                f"score={evaluation['score']} failed={failed}",
                flush=True,
            )
    return results


def _write_report(
    report: Dict[str, Any],
    output_dir: Path,
) -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "latest.json"
    markdown_path = output_dir / "latest.md"
    json_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    markdown_path.write_text(render_markdown(report), encoding="utf-8")
    return json_path, markdown_path


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="旅行规划质量评测")
    parser.add_argument(
        "--mode",
        choices=("replay", "live"),
        default="replay",
        help="replay 读取已有响应;live 调用真实后端",
    )
    parser.add_argument("--input", type=Path, help="replay 响应 JSON")
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    parser.add_argument(
        "--case",
        action="append",
        dest="case_ids",
        help="只跑指定 case,可重复传入",
    )
    parser.add_argument("--limit", type=int, help="最多运行前 N 个 case")
    parser.add_argument(
        "--backend-url",
        default="http://127.0.0.1:8000",
    )
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--list-cases", action="store_true")
    parser.add_argument(
        "--yes-live",
        action="store_true",
        help="确认 live 模式会真实消耗模型调用额度",
    )
    parser.add_argument(
        "--fail-under",
        type=float,
        default=0.0,
        help="通过率低于该值时返回退出码 1",
    )
    return parser.parse_args()


async def _main(args: argparse.Namespace) -> int:
    cases = load_golden_cases(args.cases)
    if args.list_cases:
        for case in cases:
            request = case["request"]
            print(
                f"{case['id']:<28} {case['category']:<12} "
                f"{request['city']} {request['travel_days']}天"
            )
        return 0

    selected = _select_cases(
        cases,
        case_ids=args.case_ids,
        limit=args.limit,
    )
    if not selected:
        raise ValueError("没有可运行的 case")

    if args.mode == "live":
        if not args.yes_live:
            raise ValueError(
                "live 模式会真实消耗模型额度;确认后请同时传入 --yes-live"
            )
        results = await _live_results(selected, args.backend_url)
        backend = args.backend_url
    else:
        if args.input is None:
            raise ValueError("replay 模式必须提供 --input")
        responses = _load_replay(args.input)
        results = []
        missing = []
        for case in selected:
            response = responses.get(case["id"])
            if response is None:
                missing.append(case["id"])
                continue
            results.append(evaluate_case(case, response))
        if missing:
            print(
                f"Replay 覆盖 {len(results)}/{len(selected)} 个选中 case;"
                f" 缺少响应 {len(missing)} 个。",
                flush=True,
            )
        backend = None

    if not results:
        raise ValueError("没有任何可评测的响应")

    report = build_report(results, mode=args.mode, backend=backend)
    report["generated_at"] = datetime.now(timezone.utc).isoformat()
    json_path, markdown_path = _write_report(report, args.output_dir)

    print(
        f"\n结果: {report['passed']}/{report['case_count']} 通过, "
        f"通过率 {report['pass_rate']}%, 平均分 {report['average_score']}"
    )
    print(f"JSON: {json_path}")
    print(f"Markdown: {markdown_path}")
    return 1 if report["pass_rate"] < args.fail_under else 0


def main() -> int:
    try:
        return asyncio.run(_main(_parse_args()))
    except (ValueError, RuntimeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
