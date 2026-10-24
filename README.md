<h1 align="center"><span>Codex Slack Bridge</span></h1>

+ Continue existing VS Code Codex conversations from Slack threads, with progress and completion notifications.
+ Ubuntu, Python **3.11+** with `venv`, systemd, and VS Code Codex supporting `codex queue --thread --message`.
+ Alpha release: live Slack, reboot and multi-PC operation still need validation.

<br>

## How to install

+ Follow steps **1–4 in order** on your first PC. Keep this PC running while using the Bridge. If this repository is already open in VS Code, start at **step 2**.

### 1. Get this repository

+ Run in a terminal, then keep that terminal in the downloaded folder:

```sh
git clone https://github.com/engcang/codex_slack_bridge.git
cd codex_slack_bridge
```

### 2. Create your Slack app

+ Click [Create the Slack app](https://api.slack.com/apps?new_app=1&manifest_json=%7B%22display_information%22%3A%7B%22name%22%3A%22Codex%20Slack%20Bridge%22%2C%22description%22%3A%22Continue%20existing%20Codex%20conversations%20from%20Slack%20threads%22%2C%22background_color%22%3A%22%23182430%22%7D%2C%22features%22%3A%7B%22bot_user%22%3A%7B%22display_name%22%3A%22Codex%20Bridge%22%2C%22always_online%22%3Afalse%7D%7D%2C%22oauth_config%22%3A%7B%22scopes%22%3A%7B%22bot%22%3A%5B%22chat%3Awrite%22%2C%22channels%3Ahistory%22%2C%22groups%3Ahistory%22%5D%7D%7D%2C%22settings%22%3A%7B%22event_subscriptions%22%3A%7B%22bot_events%22%3A%5B%22message.channels%22%2C%22message.groups%22%5D%7D%2C%22org_deploy_enabled%22%3Afalse%2C%22socket_mode_enabled%22%3Atrue%2C%22token_rotation_enabled%22%3Afalse%7D%7D) to open Slack with the app settings already filled in. Sign in, select your workspace, click **Next**, review the settings, and click **Create**.
+ The settings come from [src/codex_slack_bridge/templates/slack-app-manifest.json](src/codex_slack_bridge/templates/slack-app-manifest.json), a JSON file **already included in this repository**. It defines the app name, bot permissions, events and Socket Mode. You do not generate it or run the installer first.
+ If Slack does not fill in the settings: open that JSON file in VS Code, click inside it, press **Ctrl+A**, then **Ctrl+C**. Open [Slack Apps](https://api.slack.com/apps) → **Create New App → From an app manifest**, choose your workspace, select the **JSON** tab, replace the text with **Ctrl+V**, then click **Next → Create**. Paste the file's contents, not its filename or path.

### 3. Collect four Slack values

+ **Bot Token (`xoxb-...`):** on the [Slack API website](https://api.slack.com/apps), select the app you created. In the app settings page's **left sidebar**, click **OAuth & Permissions → Install to Workspace → Allow**. Copy **Bot User OAuth Token**.
+ **App Token (`xapp-...`):** on the same **Slack API app settings page**, click **Basic Information in the left sidebar**, then **App-Level Tokens → Generate Token and Scopes**. Enter a name such as `bridge`, add the `connections:write` scope, click **Generate**, and copy the token.
+ **Channel ID (`C...` or `G...`):** in the **Slack chat app or web workspace**, right-click the channel name → **Channel details → View channel details → Agents & apps → Add agents and apps**, and add **Codex Slack Bridge**. Then open the **About** tab in the same channel details window and copy **Channel ID** at the bottom.
+ **Member ID (`U...` or `W...`):** in the **Slack chat app or web workspace**, click your own profile picture in the sidebar → **Profile**. In the profile panel, click the **vertical three-dot button (⋮) immediately to the right of View as**, then **Copy member ID**.
+ Keep these values for the installer. Keep tokens private.

### 4. Run the installer

+ In a terminal in this project's folder (the folder containing `setup.sh`), as the same Ubuntu user running VS Code:

```sh
sh setup.sh
```

+ Accept or enter a PC name, then paste the four Slack values when prompted. Tokens are hidden and saved under `~/.config/codex-slack-bridge/`, outside the repository.
+ The installer creates an isolated environment, starts Router/Agent user services and enables boot startup. Ubuntu may request your sudo password for `loginctl enable-linger`; do not run the whole installer with sudo.
+ Rerun `sh setup.sh` to reinstall and restart using the existing configuration and session bindings. To change Slack values, edit `router.env` / `router.toml` in that configuration directory, then rerun setup.
+ The commands below work without activating an environment. If `~/.local/bin` is already on `PATH`, you can shorten them to `codex-slack`.

<br>

## How to use

+ Open the desired existing conversation in VS Code, run this on its PC, and select its number using the title and folder:

```sh
~/.local/bin/codex-slack add
```

+ Reply inside the new Slack message's **thread**. Only your configured Slack member ID can send commands. Repeat `add` for each new conversation; existing registrations survive restarts.
+ **Slack requests:** receive progress and the complete final response in that thread.
+ **PC requests:** ordinary chat stays local. Turns that use tools send progress and a completion notice with up to 600 characters of the final answer. No user prompts, reasoning or tool output are mirrored.
+ Progress uses Codex's commentary after tool activity, at most once per two minutes, up to 600 characters. It is an excerpt, not an AI-generated milestone summary. Tool-free PC turns do not notify.
+ Keep VS Code and the registered conversation open; boot services do not open VS Code or resume sessions. Handle permission approvals in VS Code. Use `codex-slack unbind --thread UUID` to disconnect a conversation.

<br>

## Additional PCs — optional

+ Keep one Router PC running. First configure SSH key login from the new PC to it; `ssh -oBatchMode=yes USER@ROUTER_PC true` must succeed there, with the host key already trusted.
+ On the Router PC, create a private pairing file with a unique PC name:

```sh
~/.local/bin/codex-slack pair laptop --ssh USER@ROUTER_PC --output ~/laptop-pair.json
```

+ Transfer that file securely to the new PC and run from its copy of this repository:

```sh
sh setup.sh --join ~/laptop-pair.json
~/.local/bin/codex-slack add
```

+ Setup starts its Agent and persistent SSH tunnel at boot. No additional Slack app or Slack tokens are needed. Delete the transferred pairing files after successful setup; they contain a credential.

<br>

## Check or stop

+ Check delivery: `~/.local/bin/codex-slack status` on the Router PC. Check services: `systemctl --user status codex-slack-agent codex-slack-router` (use `codex-slack-tunnel` instead of Router on another PC).
+ Read logs: `journalctl --user -u codex-slack-agent -u codex-slack-router -u codex-slack-tunnel -n 50`.
+ Stop automatic startup: `systemctl --user disable --now codex-slack-agent codex-slack-router` (substitute Tunnel on another PC). See `codex-slack --help` for delivery recovery commands.

<br>

## Cost

+ The Bridge is free under MIT and makes no additional OpenAI API calls. Codex work still consumes your existing [Codex plan or API usage](https://learn.chatgpt.com/docs/pricing).
+ [Slack Free](https://slack.com/help/articles/115002422943-Usage-limits-for-free-workspaces) supports custom apps, with limits including 10 apps and 90 days of visible message history. Your PC must remain powered and connected.
+ Built with OpenAI Codex and the Slack Python SDK; not affiliated with either project.

<br>

## LICENSE

+ [MIT License](LICENSE) · Copyright (c) 2026 Eungchang Mason Lee.
