"""Manual smoke test against the real Gemini API (Phase B completion step).

Usage:
    uv run python scripts/smoke_gemini.py <image> [<image> ...]

Deliberately excluded from pytest: this is the only place allowed to call the
real API (docs/03_tech_spec.md §1, §8). GEMINI_API_KEY comes from the process
environment, falling back to the untracked local dotenv file.
"""

import os
import sys
import time
from pathlib import Path

import httpx

from kakeibo.models import ReceiptDraft
from kakeibo.services.gemini import GeminiParseError, GeminiService

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DOTENV_PATH = PROJECT_ROOT / ".env"
API_KEY_NAME = "GEMINI_API_KEY"
CONTENT_TYPES = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".heic": "image/heic",
    ".heif": "image/heic",
}
DEFAULT_CONTENT_TYPE = "image/jpeg"
COMMENT_PREFIX = "#"
EXPORT_PREFIX = "export "
QUOTE_CHARACTERS = "\"'"
BYTES_PER_KIB = 1024
NAME_WIDTH = 30
PRICE_WIDTH = 8
CATEGORY_WIDTH = 9
UNCERTAIN_MARK = "要確認"
EXIT_OK = 0
EXIT_ERROR = 1


def parse_dotenv(path: Path) -> None:
    """Tiny KEY=VALUE reader so the script needs no extra dependency."""
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith(COMMENT_PREFIX) or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip().removeprefix(EXPORT_PREFIX).strip()
        # Real environment variables always win over the dotenv file.
        os.environ.setdefault(key, value.strip().strip(QUOTE_CHARACTERS))


def load_dotenv_file(path: Path) -> None:
    if not path.is_file():
        return
    try:
        from dotenv import load_dotenv
    except ImportError:
        parse_dotenv(path)
        return
    load_dotenv(path)


def detect_content_type(path: Path) -> str:
    return CONTENT_TYPES.get(path.suffix.lower(), DEFAULT_CONTENT_TYPE)


def print_report(draft: ReceiptDraft, elapsed_seconds: float) -> None:
    uncertain_items = [item for item in draft.items if item.uncertain]
    print(f"店名: {draft.store}")
    print(f"日付: {draft.date.isoformat()}")
    print(f"合計: {draft.total:,} 円")
    print(
        f"{'品目':<{NAME_WIDTH}}{'金額':>{PRICE_WIDTH}}  {'カテゴリ':<{CATEGORY_WIDTH}}"
    )
    for item in draft.items:
        mark = UNCERTAIN_MARK if item.uncertain else ""
        print(
            f"{item.name:<{NAME_WIDTH}}{item.price:>{PRICE_WIDTH},}  "
            f"{item.category.value:<{CATEGORY_WIDTH}}{mark}"
        )
    print(f"警告: {len(draft.warnings)} 件")
    for warning in draft.warnings:
        print(f"  - {warning}")
    print(f"要確認の品目: {len(uncertain_items)} / {len(draft.items)} 件")
    print(f"所要時間: {elapsed_seconds:.2f} 秒")


def run_image(service: GeminiService, path: Path) -> None:
    image = path.read_bytes()
    content_type = detect_content_type(path)
    size_kib = len(image) / BYTES_PER_KIB
    print(f"=== {path.name} ({content_type}, {size_kib:.0f} KiB) ===")
    started = time.perf_counter()
    draft = service.parse_receipt(image, content_type=content_type)
    print_report(draft, time.perf_counter() - started)
    print("--- raw draft JSON ---")
    print(draft.model_dump_json(indent=2))
    print()


def main(argv: list[str]) -> int:
    if not argv:
        print(
            "usage: uv run python scripts/smoke_gemini.py <image> [<image> ...]",
            file=sys.stderr,
        )
        return EXIT_ERROR

    paths = [Path(argument) for argument in argv]
    missing = [str(path) for path in paths if not path.is_file()]
    if missing:
        print(f"画像が見つかりません: {', '.join(missing)}", file=sys.stderr)
        return EXIT_ERROR

    load_dotenv_file(DOTENV_PATH)
    api_key = os.environ.get(API_KEY_NAME)
    if not api_key:
        print(f"{API_KEY_NAME} が未設定です（環境変数か .env で指定）", file=sys.stderr)
        return EXIT_ERROR

    # Construct the service exactly like AppContainer.gemini does.
    with httpx.Client() as client:
        service = GeminiService(api_key, client)
        for path in paths:
            try:
                run_image(service, path)
            except GeminiParseError as error:
                print(f"解析に失敗しました: {path.name}: {error}", file=sys.stderr)
                return EXIT_ERROR
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
