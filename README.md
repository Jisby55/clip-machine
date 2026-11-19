# Clip Machine

A local-AI autopilot for YouTube creators. It turns your own long videos and streams into vertical Shorts, writes posts in your voice, and posts to **Threads, YouTube Shorts, TikTok, Instagram and Facebook**. It also replies to comments, tracks how every post does, and learns what works for your audience.

The AI runs on your own computer through [Ollama](https://ollama.com). You don't need an AI subscription.

> **Start in safe mode.** Every platform starts **off**, and YouTube uploads stay **private** until you change that. Read [Safety](#safety) before turning on autopilot.

---

## What it does

| | |
|---|---|
| **Clip factory** | Downloads your videos, finds the loud, funny and hype moments, has the AI rate them, and renders 9:16 clips with captions, hook text and zooms. |
| **Threads bot** | Posts hot takes, reactions to news and trends, lore and series posts, and channel promos. It replies to every commenter and can join other people's posts. |
| **Multi-platform posting** | YouTube Shorts, TikTok (to your inbox until your app is approved), Instagram Reels, photos and carousels, and Facebook Reels. Posting is spaced out, with caps per day. |
| **Learning brain** | Scores each post against your own usual results. It does more of what wins, keeps testing new ideas, and remembers your regular commenters. |
| **Desktop app** | Status, queue, history, one-click "post now", a chat to control the bot in plain English, and editors for the brain files. |
| **Optional extras** | AI images, video and music (through ComfyUI), a voice clone (Chatterbox), phone alerts (through ntfy). |

## Requirements

- **Windows 10/11 or macOS** (Apple Silicon recommended)
- **Python 3.12**
- **Ollama** with a chat model. The default is `qwen3:8b` (about 5 GB). Change it in `machine_config.json`.
- **ffmpeg with libass**, needed for burned-in captions:
  - Mac: `brew install ffmpeg-full`
  - Windows: `winget install Gyan.FFmpeg`
- An NVIDIA GPU makes clips much faster on Windows, but it's optional.

## Setup

### 1. Install

**Mac:** put the folder in your home folder, for example `~/Clip-Machine`. Background jobs can't read Desktop, Documents or iCloud folders. Then run:

```bash
cd ~/Clip-Machine
bash mac/setup_mac.sh
```

**Windows:** install Python (tick "Add to PATH") and Ollama, then double-click `windows\clip-bot.bat` once. It installs everything else. Then run `windows\INSTALL-APP.bat` for a desktop shortcut.

### 2. Tell it who you are: `profile.json`

Copy `profile.example.json` to `profile.json` (Mac setup does this for you) and fill it in:

- `creator_name`, `owner_name`, `niche`
- `youtube_channel_id`: your main channel. The clips come from here.
- `threads_username`, `instagram_username`, `facebook_page_url`
- `lore`: your recurring characters and running jokes
- `games`, `outreach_topics`, `series`: optional, but they make the content feel like yours

Then make the personality yours. All of these are plain text files:

- `persona.md`: who you are and how you talk. This matters most.
- `examples.md`: a handful of posts in your voice
- `channel.md`: what your channel is about. New uploads get added automatically.
- `keywords.md`: "if someone says X, respond like Y" rules

### 3. Add your keys: `secrets.env`

Copy `secrets.env.example` to `secrets.env` and paste your keys. On a Mac, `bash mac/set-keys.sh` asks for each key without showing it on screen. **`secrets.env` is in `.gitignore`, so it never gets uploaded.**

| Platform | What you need | Where |
|---|---|---|
| **Threads** | `THREADS_TOKEN` (plus `THREADS_APP_SECRET` for the full login) | Create a Meta developer app with the **Threads API** use case. Add `https://localhost:8723/threads` as a Redirect Callback URL, then run `python mac/threads-login.py`. That login includes mentions and keyword search, which the quick "Generate Access Token" button leaves out. |
| **YouTube** | `youtube_client_secret.json` | Google Cloud Console: enable YouTube Data API v3, then create an OAuth client ID of type "Desktop app" and download it into this folder. Run `python machine.py setup-youtube`. |
| **TikTok** | `TIKTOK_CLIENT_KEY`, `TIKTOK_CLIENT_SECRET` | TikTok developer portal: create an app and add Login Kit and the Content Posting API. Until TikTok approves it, use the **Sandbox** keys (not Production) and add your account under Sandbox → Target users. Run `python machine.py setup-tiktok`. |
| **Instagram + Facebook** | `INSTAGRAM_TOKEN` | Meta app with the Instagram API and Pages use cases (a Business or Creator account linked to a Page). Run `python machine.py setup-instagram`. Facebook posting also needs `pages_manage_posts`. |

You don't need every platform. Any platform without keys is skipped.

### 4. Turn platforms on

In `machine_config.json` (created on the first run), set `"enabled": true` for each platform you've connected. The same file holds posting limits, clip length, GPU mode and the AI model.

### 5. Run it

- **Mac:** double-click `Clip Machine.command`.
- **Windows:** double-click `windows\Clip Machine.vbs`.

Try **Status** first, then **Post now** on one platform. When everything looks right, use **Autopilot: turn ON**. It runs every 15 minutes in the background.

## Custom content

**✨ Custom: upload + describe** sits under **Make content**. Pick a picture or video, type what you want, and press **Generate**. Both boxes are optional:

| Picture/video | Words | What you get |
|---|---|---|
| ✓ | ✓ | Your edits, done to your file |
| ✓ | – | The AI picks the edits |
| – | ✓ | A new AI picture or short AI video about it (the AI decides which) |
| – | – | Something random in your channel's style |

Nothing gets posted. The result is saved in `media/custom/` and opens when it's ready.

**Works out of the box** (no extra installs):
- Pictures: crop for any platform, rotate/flip, looks (B&W, vintage, vivid, warm, cool, dark, bright, dramatic), brightness/contrast/saturation, sharpen/blur, meme text.
- Videos: trim, speed, reverse, vertical 9:16, zoom, looks, mute, word-by-word captions and hook text.
- Words only, without ComfyUI: a text graphic.

**Unlocks when you install it** (the app finds it by itself; until then it does the closest thing and tells you what's missing):

| For | Install | Files (into ComfyUI's `models/` folders) |
|---|---|---|
| AI pictures and AI videos from your words | [ComfyUI Desktop](https://www.comfy.org/download) + Z-Image Turbo | `diffusion_models/z_image_turbo_bf16.safetensors`, `text_encoders/qwen_3_4b.safetensors`, `vae/ae.safetensors` |
| Picture edits that change what's in it ("remove the hat", "make it night", "turn it into a cartoon") | Qwen-Image-Edit 2509 ([files](https://huggingface.co/Comfy-Org/Qwen-Image-Edit_ComfyUI/tree/main/split_files)) | `diffusion_models/qwen_image_edit_2509_fp8_e4m3fn.safetensors`, `text_encoders/qwen_2.5_vl_7b_fp8_scaled.safetensors`, `vae/qwen_image_vae.safetensors`. Optional 4-step speed-up (from lightx2v/Qwen-Image-Lightning): `loras/Qwen-Image-Lightning-4steps-V1.0.safetensors` |
| Turning a video into something else (anime, clay, new scene) and fully-AI videos | Wan 2.2 image-to-video ([files](https://huggingface.co/Comfy-Org/Wan_2.2_ComfyUI_Repackaged/tree/main/split_files)) | the files listed in `WAN` at the top of `mm_aivideo.py` |
| Background music | the music model `mm_audio.py` uses | see `mm_audio.py` |
| Letting the AI *see* your upload when you leave the words empty | any Ollama vision model, e.g. `ollama pull qwen2.5vl:7b` | found automatically, or set `"vision_model"` in `machine_config.json` |

Using different file names? Set `image_edit_model`, `image_edit_clip`, `image_edit_vae` or `image_edit_lora` in `machine_config.json`.

From the command line: `python machine.py custom [picture or video] [what you want]`.

## Safety

- **Only run one copy at a time.** Two machines on the same accounts will double-post.
- Posting is spaced out with random timing. Each platform starts at one post a day and ramps up only if the last five posts beat the five before. Hard caps apply no matter what the config says.
- **Built-in content rules that can't be turned off:** no slurs, no hate toward groups, no harassing real people, no made-up accusations, and no jokes about real tragedies. Every draft passes a word filter and an AI checker before it posts.
- YouTube uploads stay private until your API audit passes (`"audited": true`). TikTok goes to your inbox until your app is approved.
- Put "content by AI" (or similar) in your bios. The bot never claims to be human when someone sincerely asks.
- Outreach (joining other people's posts) and auto-replying to Instagram DMs are opt-in. Follow each platform's rules on automation.

## Files

```
app.py, app_views.py        desktop app
machine.py                  the scheduler: one "tick" runs everything
threadsbot.py               Threads + the shared writing brain
mm_*.py                     one module per job (video, youtube, tiktok, instagram, brain, safety, ...)
creator_profile.py          loads profile.json (no creator details are hard-coded anywhere)
mac/, windows/              setup, launch and autopilot scripts per OS
persona.md, examples.md, channel.md, keywords.md   your bot's personality (edit freely)
```

These are created as it runs and are never committed: `secrets.env`, `profile.json`, `machine_config.json`, `*_token.json`, `threadsbot.db`, `memory.md`, `media/`, `bot.log`.

## License

MIT. See [LICENSE](LICENSE).
