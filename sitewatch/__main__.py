import argparse
import json
from pathlib import Path
import webbrowser

from .demo import sample
from .transport import fetch_file, fetch_ssh, fetch_update_log, run_update, ssh_command
from .viewer import State, make_server


def main():
    parser = argparse.ArgumentParser(description="WP Site Watch local dashboard")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--demo", action="store_true", help="Use clearly labelled sample data")
    source.add_argument("--file", type=Path, help="Read a collector snapshot")
    source.add_argument("--config", type=Path, help="Read SSH connection configuration")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()
    updates = None
    try:
        if args.demo:
            loader, label = sample, "Demo · sample data"
        elif args.file:
            loader, label = lambda: fetch_file(args.file), "Local snapshot"
        else:
            config = json.loads(args.config.read_text(encoding="utf-8"))
            ssh_command(config)
            loader, label = lambda: fetch_ssh(config), "SSH · read only"
            if config.get("updates") is True:
                updates = (lambda site, kind, slug: run_update(config, site, kind, slug), lambda: fetch_update_log(config))
                label = "SSH · updates enabled"
        state = State(loader, label, updates)
        state.refresh()
        server = make_server(state, args.port)
    except (ValueError, OSError) as exc:
        parser.error(str(exc))
    url = f"http://127.0.0.1:{server.server_port}"
    print(f"WP Site Watch: {url}\nPress Ctrl+C to stop.", flush=True)
    if not args.no_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
