"""
DSA_Agent_III — interactive CLI (AMD-native, Lemonade 11.0).

Usage:
    python cli.py                  # auto-route every query
    python cli.py --platform aws   # lock to one platform

Commands (type during chat):
    /platform auto|databricks|snowflake|aws   switch platform
    /speak                                    speak the last response (Lemonade TTS)
    /models                                   show NPU + iGPU model status
    /save [filename]                          save last response to Downloads as .md
    /copy                                     copy last response (raw markdown) to clipboard
    /history [N]                              list the last N persisted turns (default 10)
    /search <keyword>                         full-text search across all past Q&A
    /replay <id>                              re-render a stored turn by its id
    /reset                                    clear in-session conversation history
    /quit  or  exit  or  Ctrl-C              exit
"""
from __future__ import annotations

import argparse
import os
import sys
import threading
import urllib.error
import urllib.request

from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.text import Text

from core import config

console = Console()


def _lemonade_is_up(timeout: float = 2.0) -> bool:
    """Health probe against the Lemonade server (OpenAI /v1/models surface)."""
    url = f"{config.LEMONADE_BASE_URL.rstrip('/')}/models"
    try:
        req = urllib.request.Request(
            url, headers={"Authorization": f"Bearer {config.LEMONADE_API_KEY}"}
        )
        with urllib.request.urlopen(req, timeout=timeout):
            return True
    except urllib.error.HTTPError:
        # A 4xx still means the server is answering.
        return True
    except (urllib.error.URLError, ConnectionError, TimeoutError, OSError):
        return False


def _ensure_lemonade_running() -> bool:
    if _lemonade_is_up():
        return True
    console.print(
        f"[red]Lemonade server is not reachable at {config.native_base_url()}.[/red]\n"
        "Start it (installed as a Windows service on :13305) or run "
        "[cyan].\\infra\\01_install_lemonade.ps1[/cyan] first."
    )
    return False


def _warm_models(hybrid) -> None:
    console.print("[dim]Warming models (iGPU coder + NPU router)...[/dim]", end="\r")
    results = hybrid.warm()
    console.print(" " * 60, end="\r")
    for name, ok in results.items():
        if not ok:
            console.print(
                f"[yellow]Note: could not warm '{name}' — pull/load it or set the "
                f"correct name in .env. First use may be slow.[/yellow]"
            )


def _warm_rag():
    console.print("[dim]Warming up embedding model...[/dim]", end="\r")
    from rag.retriever import _get_chroma_client, _get_embed_model
    _get_embed_model()
    _get_chroma_client()
    console.print(" " * 40, end="\r")


def _auto_refresh_docs():
    """Best-effort background doc refresh; never blocks startup (see II)."""
    if os.getenv("RAG_AUTO_REFRESH", "1") == "0":
        return

    def _worker():
        try:
            from rag import refresher
            summary = refresher.auto_refresh()
            changed = summary.get("changed") or []
            if changed:
                console.print(
                    f"[dim green][rag] docs updated to latest: "
                    f"{', '.join(changed)}[/dim green]"
                )
        except Exception:
            pass

    threading.Thread(target=_worker, daemon=True).start()


def _banner(platform: str):
    console.print(Panel(
        "[bold cyan]DSA Agent III[/bold cyan] — AMD-native on Lemonade 11.0\n"
        "Qwen3-Coder-30B (iGPU/ROCm) + NPU hybrid · Ryzen AI Max+ 395\n"
        f"Platform: [bold yellow]{platform}[/bold yellow]  "
        "[dim]| /platform <name>  /speak  /models  /reset  /quit[/dim]",
        expand=False,
    ))


def _print_result(result: dict):
    platform = result.get("platform", "?")
    latency  = result.get("latency_ms", 0)
    tools    = result.get("tool_calls_made", [])
    sources  = result.get("rag_sources", [])

    console.print(Markdown(result["response"]))

    meta = Text()
    meta.append(f"  platform={platform}", style="dim cyan")
    meta.append(f"  {latency}ms", style="dim")
    if tools:
        names = ", ".join(t["name"] for t in tools)
        meta.append(f"  tools=[{names}]", style="dim green")
    if sources:
        files = ", ".join(
            s["source"].replace("\\", "/").split("/")[-1]
            for s in sources[:3]
        )
        meta.append(f"  rag=[{files}]", style="dim magenta")
    console.print(meta)
    console.print()


def _run_cancellable(pipeline, query: str, override) -> dict | None:
    """Run pipeline.run() in a daemon thread so Ctrl-C stays responsive."""
    box: dict = {}

    def _worker():
        try:
            box["result"] = pipeline.run(query, platform_override=override)
        except Exception as exc:
            box["error"] = exc

    t = threading.Thread(target=_worker, daemon=True)
    t.start()

    with console.status(
        "[bold yellow]thinking...[/bold yellow]  [dim](Ctrl-C to cancel)[/dim]"
    ):
        try:
            while t.is_alive():
                t.join(timeout=0.2)
        except KeyboardInterrupt:
            console.print("[yellow]Cancelled. (server request may finish in background.)[/yellow]")
            return None

    if "error" in box:
        raise box["error"]
    return box.get("result")


def _print_models(hybrid):
    from rich.table import Table
    t = Table(show_header=True, header_style="bold cyan", expand=False)
    t.add_column("model")
    t.add_column("role", style="cyan")
    t.add_column("device", style="magenta")
    t.add_column("loaded", style="dim")
    for m in hybrid.status():
        loaded = m["loaded"]
        mark = "?" if loaded is None else ("yes" if loaded else "no")
        t.add_row(m["name"], m["role"], m.get("device") or "-", mark)
    console.print(t)


def _print_history_table(turns: list[dict]):
    if not turns:
        console.print("[dim]No history yet.[/dim]")
        return
    from rich.table import Table
    t = Table(show_header=True, header_style="bold cyan", expand=False)
    t.add_column("id", justify="right", style="dim")
    t.add_column("when", style="dim")
    t.add_column("platform", style="cyan")
    t.add_column("ms", justify="right", style="dim")
    t.add_column("query", overflow="fold")
    for r in turns:
        when = (r.get("ts") or "").replace("T", " ")
        q    = (r.get("query") or "").strip().replace("\n", " ")
        if len(q) > 80:
            q = q[:77] + "..."
        t.add_row(
            str(r.get("id", "")),
            when,
            str(r.get("platform", "")),
            str(r.get("latency_ms", 0)),
            q,
        )
    console.print(t)


def main():
    parser = argparse.ArgumentParser(description="DSA Agent III CLI")
    parser.add_argument(
        "--platform",
        choices=["auto", "databricks", "snowflake", "aws"],
        default="auto",
        help="Lock to a platform or let the router decide (default: auto)",
    )
    parser.add_argument(
        "--no-history",
        action="store_true",
        help="Disable persistent Q&A history for this session",
    )
    args = parser.parse_args()
    current_platform = args.platform

    if not _ensure_lemonade_running():
        console.print("[red]Cannot continue without Lemonade. Exiting.[/red]")
        sys.exit(1)

    from core.hybrid import HybridManager
    hybrid = HybridManager()

    _auto_refresh_docs()   # background: keep RAG corpus current, never blocks
    _warm_rag()
    _warm_models(hybrid)

    from orchestrator.pipeline import AgentPipeline
    pipeline = AgentPipeline()

    history_enabled = not args.no_history and os.environ.get("DSA_HISTORY", "1") != "0"
    from orchestrator.history import History
    history = History(enabled=history_enabled)
    if history_enabled:
        console.print(
            f"[dim]History: {history.db_path} "
            f"({history.count()} turns recorded)[/dim]"
        )

    _banner(current_platform)

    last_query: str | None = None
    last_result: dict | None = None

    while True:
        try:
            user_input = console.input("[bold green]you>[/bold green] ").strip()
        except (EOFError, KeyboardInterrupt):
            console.print("\n[dim]Bye.[/dim]")
            sys.exit(0)

        if not user_input:
            continue

        # --- built-in commands ---
        if user_input.lower() in ("/quit", "exit", "quit"):
            console.print("[dim]Bye.[/dim]")
            sys.exit(0)

        if user_input.lower() == "/reset":
            pipeline.reset()
            console.print("[dim]Session history cleared.[/dim]")
            continue

        if user_input.lower() == "/models":
            _print_models(hybrid)
            continue

        if user_input.lower() == "/speak":
            if last_result is None:
                console.print("[yellow]Nothing to speak yet — ask a question first.[/yellow]")
                continue
            from modalities import tts
            try:
                path = tts.speak(last_result.get("response", "") or "")
                console.print(f"[green]Speaking:[/green] {path}")
            except Exception as exc:
                console.print(f"[red]TTS failed: {exc}[/red]")
            continue

        if user_input.lower() == "/save" or user_input.lower().startswith("/save "):
            if last_result is None:
                console.print("[yellow]Nothing to save yet — ask a question first.[/yellow]")
                continue
            from orchestrator.exporter import save_turn
            parts = user_input.split(maxsplit=1)
            fname = parts[1].strip() if len(parts) == 2 else None
            try:
                path = save_turn(last_query or "", last_result, filename=fname)
                console.print(f"[green]Saved:[/green] {path}")
            except Exception as exc:
                console.print(f"[red]Save failed: {exc}[/red]")
            continue

        if user_input.lower() == "/copy":
            if last_result is None:
                console.print("[yellow]Nothing to copy yet — ask a question first.[/yellow]")
                continue
            from orchestrator.exporter import copy_to_clipboard
            text = last_result.get("response", "") or ""
            if copy_to_clipboard(text):
                console.print(f"[green]Copied {len(text)} chars to clipboard.[/green]")
            else:
                console.print("[red]Clipboard copy failed — no clipboard utility found.[/red]")
            continue

        if user_input.lower() == "/history" or user_input.lower().startswith("/history "):
            parts = user_input.split(maxsplit=1)
            try:
                n = int(parts[1]) if len(parts) == 2 else 10
            except ValueError:
                console.print("[red]Usage: /history [N][/red]")
                continue
            _print_history_table(history.recent(limit=n))
            continue

        if user_input.lower().startswith("/search "):
            keyword = user_input.split(maxsplit=1)[1].strip()
            if not keyword:
                console.print("[red]Usage: /search <keyword>[/red]")
                continue
            matches = history.search(keyword, limit=20)
            if not matches:
                console.print(f"[yellow]No matches for '{keyword}'.[/yellow]")
            else:
                console.print(f"[dim]{len(matches)} match(es) for '{keyword}':[/dim]")
                _print_history_table(matches)
            continue

        if user_input.lower().startswith("/replay "):
            parts = user_input.split(maxsplit=1)
            try:
                turn_id = int(parts[1].strip())
            except (IndexError, ValueError):
                console.print("[red]Usage: /replay <id>[/red]")
                continue
            turn = history.get(turn_id)
            if not turn:
                console.print(f"[yellow]No turn with id {turn_id}.[/yellow]")
                continue
            console.print(f"[dim]Replaying turn #{turn_id} from {turn['ts']}[/dim]")
            console.print(f"[bold green]Q:[/bold green] {turn['query']}")
            console.print()
            from orchestrator.history import turn_to_result
            _print_result(turn_to_result(turn) | {"platform": turn["platform"]})
            last_query  = turn["query"]
            last_result = turn_to_result(turn) | {"platform": turn["platform"]}
            continue

        if user_input.lower().startswith("/platform "):
            chosen = user_input.split(maxsplit=1)[1].strip().lower()
            if chosen in ("auto", "databricks", "snowflake", "aws"):
                current_platform = chosen
                console.print(f"[dim]Platform set to [bold]{current_platform}[/bold][/dim]")
            else:
                console.print("[red]Unknown platform. Choose: auto databricks snowflake aws[/red]")
            continue

        # --- agent query ---
        override = None if current_platform == "auto" else current_platform
        try:
            result = _run_cancellable(pipeline, user_input, override)
        except Exception as exc:
            console.print(f"[red]Error: {exc}[/red]")
            continue
        if result is None:
            continue

        last_query = user_input
        last_result = result
        _print_result(result)

        try:
            history.append(user_input, result)
        except Exception as exc:
            console.print(f"[dim red]History append failed: {exc}[/dim red]")


if __name__ == "__main__":
    main()
