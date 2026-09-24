# App Switchboard

A small browser dashboard to start, stop and watch your local Flask, Streamlit and Django apps, without opening a terminal and `cd`-ing into each folder. It runs on Windows, macOS and Linux.

## Project layout

```
app-switchboard/
├── run.py                    # entry point: python run.py
├── requirements.txt
├── switchboard/              # the application package
│   ├── __init__.py
│   ├── __main__.py           # also runnable as: python -m switchboard
│   ├── server.py             # Flask API + process management
│   └── static/
│       └── index.html        # the dashboard UI
├── config/
│   ├── apps.example.json     # sample app list (committed)
│   └── apps.json             # your app list (created on first run, git-ignored)
├── scripts/
│   ├── start_windows.bat     # double-click launcher, no console window
│   └── start_mac_linux.sh    # background launcher
├── data/                     # runtime state: PIDs of running apps (git-ignored)
└── logs/                     # one log file per app (git-ignored)
```

## Setup (once)

```
git clone <your-repo-url> app-switchboard
cd app-switchboard
pip install -r requirements.txt
python run.py
```

Your browser opens at http://127.0.0.1:5050. From then on, use a launcher instead of a terminal:

- **Windows:** double-click `scripts\start_windows.bat`. You can also right-click it and choose Send to > Desktop (create shortcut).
- **macOS / Linux:** run `scripts/start_mac_linux.sh`, or point a desktop shortcut or login item at it.

Add `--no-browser` to `python run.py` if you don't want a tab opened automatically.

## Adding an app

Click **Add app** and fill in:

| Field | Example | Notes |
|---|---|---|
| App folder | `C:\Projects\sales` | The folder you'd normally `cd` into |
| Framework | Streamlit | Flask, Streamlit, Django or Custom |
| Entry file | `app.py` | For Flask this can also be `myapp:create_app()` |
| Port | 8501 | Each app needs its own port. The next free one is suggested. |
| Virtualenv folder | `.venv` | Relative to the app folder. Leave it empty to use the switchboard's Python. |
| Environment variables | `FLASK_DEBUG=1` | One `KEY=VALUE` per line |

These are the commands it runs for you:

- Flask: `python -m flask --app <entry> run --port <port>` (needs Flask 2.2 or newer)
- Streamlit: `python -m streamlit run <entry> --server.port <port> --server.headless true`
- Django: `python manage.py runserver 127.0.0.1:<port>`
- Custom: anything you like, e.g. `{python} -m uvicorn main:app --port {port}`

Hover over an app's folder path to see the exact command.

Apps are saved in `config/apps.json`, which you can also edit by hand. See `config/apps.example.json` for the format. This file is git-ignored because its folder paths are specific to your machine.

## Good to know

- **Virtualenvs matter.** If an app has its own venv, set it. Otherwise that app's packages must be installed in the Python that runs the switchboard.
- **Apps outlive the switchboard.** If you close the switchboard, your apps keep running, and it reconnects to them when you reopen it. Use **Stop all** if you want everything off.
- **Stopping is thorough.** Stop kills the app and every process it spawned, including Flask/Django auto-reloaders, Streamlit workers and Windows venv launchers, so ports are actually freed.
- **Logs** for each app are in `logs/<app-id>.log` and in the Logs panel. Files over 5 MB are rotated when the app next starts.
- **"Stopped unexpectedly"** means the app crashed or exited by itself. Open its logs to see why.
- **"Port … is used by another program"** means something outside the switchboard, such as an app you started from a terminal, is holding that port.
- **Configuration:** the switchboard only listens on 127.0.0.1, so other machines can't reach it. Change this with the `SWITCHBOARD_HOST` / `SWITCHBOARD_PORT` environment variables, but it has no login, so don't expose it on a network.
