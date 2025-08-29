import os
import re
import asyncio
import time
import random
import math
from dataclasses import dataclass, field
from typing import Optional, List, Tuple, Literal

import discord
from discord import app_commands
from discord.ext import commands

# --- Third-party helpers ---
# Ensure you installed: pip install -U discord.py[voice] yt-dlp spotipy python-dotenv
import yt_dlp
from dotenv import load_dotenv

# Spotify (optional). If not configured, we still work via YouTube.
try:
    import spotipy
    from spotipy.oauth2 import SpotifyClientCredentials
    SPOTIFY_AVAILABLE = True
except Exception:
    SPOTIFY_AVAILABLE = False

# ------------- Config -------------
load_dotenv()
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
FFMPEG_PATH = os.getenv("FFMPEG_PATH", "ffmpeg")  # Put full path on Windows if not in PATH

GUILD_ID_ENV = os.getenv("GUILD_ID")
GUILD_ID: Optional[int] = int(GUILD_ID_ENV) if GUILD_ID_ENV and GUILD_ID_ENV.isdigit() else None

SPOTIFY_CLIENT_ID = os.getenv("SPOTIFY_CLIENT_ID")
SPOTIFY_CLIENT_SECRET = os.getenv("SPOTIFY_CLIENT_SECRET")

YTDL_OPTS = {
    "format": "bestaudio[ext=m4a]/bestaudio/best",
    "quiet": True,
    "noplaylist": True,
    "default_search": "ytsearch",
    "source_address": "0.0.0.0",
    # Use Android client to avoid SABR Missing URL; we also add fallbacks
    "extractor_args": {"youtube": {"player_client": ["android"]}},
    "ignore_no_formats_error": True,
}

FFMPEG_OPTS = {
    "before_options": "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5",
    "options": "-vn",
}

# ------------- Data models -------------
@dataclass
class Track:
    title: str
    url: str
    webpage_url: str
    requested_by_name: str
    requested_by_id: int
    duration: Optional[int] = None

@dataclass
class GuildPlayer:
    voice: Optional[discord.VoiceClient] = None
    queue: asyncio.Queue = field(default_factory=asyncio.Queue)
    now_playing: Optional[Track] = None
    player_task: Optional[asyncio.Task] = None
    volume: float = 0.5
    announce_channel_id: Optional[int] = None  # where to announce Now Playing
    loop_one: bool = False
    loop_all: bool = False
    play_started_at: Optional[float] = None

    async def ensure_player(self, bot: commands.Bot, guild: discord.Guild) -> None:
        if self.player_task is None or self.player_task.done():
            self.player_task = asyncio.create_task(self._player_loop(bot, guild))

    async def _player_loop(self, bot: commands.Bot, guild: discord.Guild) -> None:
        while True:
            track: Track = await self.queue.get()
            self.now_playing = track
            if not self.voice or not self.voice.is_connected():
                self.now_playing = None
                continue

            # Update bot presence
            await update_presence(track.title)

            next_event = asyncio.Event()

            def after_playback(_):
                bot.loop.call_soon_threadsafe(next_event.set)

            try:
                self.play_started_at = time.monotonic()
                source = discord.FFmpegPCMAudio(track.url, executable=FFMPEG_PATH, **FFMPEG_OPTS)
                audio = discord.PCMVolumeTransformer(source, volume=self.volume)
                self.voice.play(audio, after=after_playback)
            except Exception as e:
                print(f"Playback error: {e} — mencoba re-ekstrak dengan klien lain…")
                # Fallback: re-extract with different client profiles
                try:
                    title, stream, page, duration = await extract_from_youtube(track.webpage_url)
                    track.title, track.url, track.webpage_url, track.duration = title, stream, page, duration
                    source = discord.FFmpegPCMAudio(track.url, executable=FFMPEG_PATH, **FFMPEG_OPTS)
                    audio = discord.PCMVolumeTransformer(source, volume=self.volume)
                    self.voice.play(audio, after=after_playback)
                except Exception as ee:
                    print(f"Fallback playback failed: {ee}")
                    next_event.set()

            await next_event.wait()
            self.now_playing = None
            # If loop is enabled, re-queue accordingly
            if self.loop_one:
                try:
                    self.queue._queue.appendleft(track)  # type: ignore[attr-defined]
                except Exception:
                    await self.queue.put(track)
            elif self.loop_all:
                await self.queue.put(track)
            if self.queue.empty():
                await update_presence(None)

# ------------- Helpers -------------
YOUTUBE_URL_RE = re.compile(r"(https?://)?(www\.)?(youtube\.com|youtu\.be)/.+")
YOUTUBE_PLAYLIST_RE = re.compile(r"(?:https?://)?(?:www\.)?(?:youtube\.com|youtu\.be)/.*(?:[?&])list=([A-Za-z0-9_-]+)")
SPOTIFY_URL_RE = re.compile(r"https?://open\.spotify\.com/(track|playlist)/[A-Za-z0-9]+")

_spotify_client: Optional["spotipy.Spotify"] = None

def get_spotify_client() -> Optional["spotipy.Spotify"]:
    global _spotify_client
    if not SPOTIFY_AVAILABLE:
        return None
    if SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET:
        if _spotify_client is None:
            auth_mgr = SpotifyClientCredentials(client_id=SPOTIFY_CLIENT_ID, client_secret=SPOTIFY_CLIENT_SECRET)
            _spotify_client = spotipy.Spotify(auth_manager=auth_mgr)
        return _spotify_client
    return None

async def extract_from_youtube(query: str) -> Tuple[str, str, str, Optional[int]]:
    """Return (title, stream_url, webpage_url) from a YouTube URL or search query.
    Tries multiple player clients to avoid SABR/403 and manually selects audio-only format if needed.
    """
    loop = asyncio.get_running_loop()

    async def _try_with_clients(clients: List[str]):
        def _extract_once(client: str):
            local_opts = YTDL_OPTS.copy()
            local_opts["extractor_args"] = {"youtube": {"player_client": [client]}}
            with yt_dlp.YoutubeDL(local_opts) as ydl:
                if YOUTUBE_URL_RE.match(query):
                    info = ydl.extract_info(query, download=False)
                else:
                    info = ydl.extract_info(f"ytsearch:{query}", download=False)
                    if "entries" in info and info["entries"]:
                        info = info["entries"][0]
                stream_url = info.get("url")
                if not stream_url:
                    fmts = info.get("formats") or []
                    audio_fmts = [
                        f for f in fmts
                        if (f.get("vcodec") in ("none", None)) and (f.get("acodec") not in ("none", None)) and f.get("url")
                    ]
                    def _key(f):
                        ext = f.get("ext") or ""
                        abr = f.get("abr") or 0
                        return (ext != "m4a", -abr)
                    audio_fmts.sort(key=_key)
                    if audio_fmts:
                        stream_url = audio_fmts[0]["url"]
                duration = info.get("duration")
                if duration is not None:
                    try:
                        duration = int(duration)
                    except Exception:
                        duration = None
                return info.get("title", "Unknown"), stream_url, info.get("webpage_url", info.get("original_url", query)), duration

        last_err = None
        for c in clients:
            try:
                return await loop.run_in_executor(None, _extract_once, c)
            except Exception as e:
                last_err = e
                continue
        if last_err:
            raise last_err
        raise RuntimeError("Ekstraksi gagal.")

    title, stream_url, webpage_url, duration = await _try_with_clients(["android", "web", "tv"])
    if not stream_url:
        raise RuntimeError("Tidak dapat mengekstrak audio dari YouTube.")
    return title, stream_url, webpage_url, duration

async def resolve_spotify_to_query(url: str) -> List[str]:
    sp = get_spotify_client()
    if sp is None:
        # Fallback: just return as-is so it will search on YouTube
        return [url]

    items: List[str] = []
    if "/track/" in url:
        track = sp.track(url)
        name = track["name"]
        artists = ", ".join(a["name"] for a in track["artists"])
        items.append(f"{name} {artists} audio")
    elif "/playlist/" in url:
        results = sp.playlist_items(url, additional_types=("track",))
        while results:
            for it in results["items"]:
                t = it.get("track")
                if not t:
                    continue
                name = t.get("name")
                artists = ", ".join(a.get("name") for a in t.get("artists", []))
                items.append(f"{name} {artists} audio")
            results = sp.next(results) if results.get("next") else None
    else:
        items.append(url)
    return items

async def expand_youtube_playlist(url: str, limit: int = 50) -> List[str]:
    """Return a list of video watch URLs from a YouTube playlist (flat, no download)."""
    loop = asyncio.get_running_loop()

    def _extract():
        opts = YTDL_OPTS.copy()
        opts["extract_flat"] = True
        opts["noplaylist"] = False
        opts["playlistend"] = limit
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
            entries = info.get("entries") or []
            urls: List[str] = []
            for e in entries:
                u = e.get("url") or e.get("id")
                if not u:
                    continue
                if not u.startswith("http"):
                    u = f"https://www.youtube.com/watch?v={u}"
                urls.append(u)
            return urls

    try:
        return await loop.run_in_executor(None, _extract)
    except Exception:
        return []

async def make_track(query_or_url: str, requested_by_id: int, requested_by_name: str) -> Track:
    # Accept YouTube URL, plain query, or Spotify URL (resolve to YouTube search)
    if SPOTIFY_URL_RE.match(query_or_url):
        queries = await resolve_spotify_to_query(query_or_url)
        first = queries[0]
        title, stream, page, duration = await extract_from_youtube(first)
        return Track(title=title, url=stream, webpage_url=page, requested_by_name=requested_by_name, requested_by_id=requested_by_id, duration=duration)
    else:
        title, stream, page, duration = await extract_from_youtube(query_or_url)
        return Track(title=title, url=stream, webpage_url=page, requested_by_name=requested_by_name, requested_by_id=requested_by_id, duration=duration)

# ------------- Bot setup -------------
intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix=commands.when_mentioned_or("!"), intents=intents)

guild_players: dict[int, GuildPlayer] = {}

# Presence helper — show current track in bot status
async def update_presence(title: Optional[str]) -> None:
    try:
        if title:
            await bot.change_presence(activity=discord.Activity(type=discord.ActivityType.listening, name=title))
        else:
            await bot.change_presence(activity=discord.Game(name="/play to play music"))
    except Exception:
        pass

def get_player(guild_id: int) -> GuildPlayer:
    gp = guild_players.get(guild_id)
    if gp is None:
        gp = GuildPlayer()
        guild_players[guild_id] = gp
    return gp

# ------------- Voice helpers -------------
async def ensure_voice(interaction: discord.Interaction) -> GuildPlayer:
    assert interaction.guild is not None
    gp = get_player(interaction.guild.id)

    if gp.voice and gp.voice.is_connected():
        return gp

    if not interaction.user or not isinstance(interaction.user, discord.Member):
        raise RuntimeError("Could not find your voice channel.")

    if not interaction.user.voice or not interaction.user.voice.channel:
        raise RuntimeError("You must join a voice channel first.")

    channel = interaction.user.voice.channel
    gp.voice = await channel.connect()
    return gp

# ------------- Slash commands -------------
@bot.event
async def on_ready():
    try:
        # Global sync (commands available in all servers; may take time to propagate)
        synced_global = await bot.tree.sync()
        print(f"Synced {len(synced_global)} global commands")

        # Fast per-guild sync for every guild the bot is currently in (instant availability)
        for g in bot.guilds:
            try:
                guild_obj = discord.Object(id=g.id)
                bot.tree.copy_global_to(guild=guild_obj)
                sg = await bot.tree.sync(guild=guild_obj)
                print(f"Synced {len(sg)} commands to guild {g.id}")
            except Exception as ge:
                print(f"Guild sync failed for {g.id}: {ge}")
    except Exception as e:
        print(f"Slash sync failed: {e}")
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")
    # Set idle presence on startup
    await update_presence(None)

@bot.tree.command(name="ping", description="Check if the bot is responsive")
async def ping(interaction: discord.Interaction):
    await interaction.response.send_message("Pong! ✅")

@bot.tree.command(name="join", description="Have the bot join your voice channel")
async def join(interaction: discord.Interaction):
    try:
        await interaction.response.defer(ephemeral=True)
        gp = await ensure_voice(interaction)
        await interaction.followup.send(f"✅ Joined: {gp.voice.channel}", ephemeral=True)
    except Exception as e:
        await interaction.response.send_message(f"❌ {e}", ephemeral=True)

@bot.tree.command(name="play", description="Play a song from YouTube or a Spotify URL / title")
@app_commands.describe(query="e.g., a Spotify link or 'Someone Like You Adele'")
async def play(interaction: discord.Interaction, query: str):
    assert interaction.guild is not None
    await interaction.response.defer()
    try:
        gp = await ensure_voice(interaction)
        gp.announce_channel_id = interaction.channel.id if interaction.channel else None

        # Auto-detect YouTube playlist and expand into queue
        if YOUTUBE_PLAYLIST_RE.search(query):
            video_urls = await expand_youtube_playlist(query, limit=50)
            if video_urls:
                # Find first playable track
                first_track: Optional[Track] = None
                first_idx: int = -1
                for idx, vu in enumerate(video_urls):
                    try:
                        cand = await make_track(vu, requested_by_id=interaction.user.id, requested_by_name=interaction.user.display_name)
                        await gp.queue.put(cand)
                        first_track = cand
                        first_idx = idx
                        break
                    except Exception:
                        continue  # skip unavailable video

                if first_track is None:
                    await interaction.followup.send("❌ Couldn't queue any playable tracks from that playlist.")
                    return

                # Enqueue the rest, skipping any unavailable ones
                enqueued_rest = 0
                for j, vu in enumerate(video_urls):
                    if j == first_idx:
                        continue
                    try:
                        t = await make_track(vu, requested_by_id=interaction.user.id, requested_by_name=interaction.user.display_name)
                        await gp.queue.put(t)
                        enqueued_rest += 1
                    except Exception:
                        continue

                await gp.ensure_player(bot, interaction.guild)
                more = f"… ({enqueued_rest}) more" if enqueued_rest else ""
                msg = (
                    "✅ Playlist detected. Now queued to start with: "
                    f"\n**[{first_track.title}]({first_track.webpage_url})** — requested by <@{interaction.user.id}>"
                    + more
                )
                await interaction.followup.send(msg)
                return
            else:
                await interaction.followup.send("❌ Couldn't read that playlist URL.")
                return
            # fallback to normal if extraction failed

        # Normal single track / URL / query flow
        track = await make_track(query, requested_by_id=interaction.user.id, requested_by_name=interaction.user.display_name)
        await gp.queue.put(track)
        await gp.ensure_player(bot, interaction.guild)
        await interaction.followup.send(
            f"✅ Added: **[{track.title}]({track.webpage_url})** — requested by <@{track.requested_by_id}>"
        )
    except Exception as e:
        await interaction.followup.send(f"❌ Failed to add track: {e}")

@bot.tree.command(name="spotify", description="Play from Spotify (URL track/playlist or title)")
@app_commands.describe(query="Contoh: link Spotify atau 'Someone Like You Adele'")
async def spotify_cmd(interaction: discord.Interaction, query: str):
    assert interaction.guild is not None
    await interaction.response.defer()
    try:
        gp = await ensure_voice(interaction)
        # remember channel for now-playing announcements
        gp.announce_channel_id = interaction.channel.id if interaction.channel else None

        # URL Spotify (track/playlist)
        if SPOTIFY_URL_RE.match(query):
            queries = await resolve_spotify_to_query(query)
            titles_links: List[Tuple[str, str]] = []
            for q in queries[:50]:  # limit to avoid spam
                try:
                    t = await make_track(q, requested_by_id=interaction.user.id, requested_by_name=interaction.user.display_name)
                    await gp.queue.put(t)
                    titles_links.append((t.title, t.webpage_url))
                except Exception:
                    continue
            await gp.ensure_player(bot, interaction.guild)
            added = len(titles_links)
            if added:
                preview = "".join(f"- [{title}]({url})" for title, url in titles_links[:5])
                more = "" if added <= 5 else f"…and {added-5} more"
                msg = f"✅ Added {added} tracks from Spotify:{preview}{more}"
            else:
                msg = "❌ Nothing could be added."
            await interaction.followup.send(msg)
            return

        # Pencarian judul di Spotify → resolve ke YouTube
        sp = get_spotify_client()
        if sp is None:
            await interaction.followup.send("❌ Spotify is not configured. Set SPOTIFY_CLIENT_ID/SECRET in .env.")
            return
        res = sp.search(q=query, type="track", limit=1)
        items = res.get("tracks", {}).get("items", [])
        if not items:
            await interaction.followup.send("❌ No results found on Spotify.")
            return
        tt = items[0]
        name = tt.get("name")
        artists = ", ".join(a.get("name") for a in tt.get("artists", []))
        yquery = f"{name} {artists} audio"
        t = await make_track(yquery, requested_by_id=interaction.user.id, requested_by_name=interaction.user.display_name)
        await gp.queue.put(t)
        await gp.ensure_player(bot, interaction.guild)
        await interaction.followup.send(f"✅ Added dari Spotify: **[{name} — {artists}]({t.webpage_url})** — requested by <@{interaction.user.id}>")
    except Exception as e:
        await interaction.followup.send(f"❌ Failed to process Spotify: {e}")

@bot.tree.command(name="skip", description="Skip the current song")
async def skip(interaction: discord.Interaction):
    assert interaction.guild is not None
    gp = get_player(interaction.guild.id)
    if gp.voice and gp.voice.is_playing():
        gp.voice.stop()
        await interaction.response.send_message("⏭️ Skipped.")
    else:
        await interaction.response.send_message("Nothing is playing.", ephemeral=True)

@bot.tree.command(name="pause", description="Pause the song")
async def pause(interaction: discord.Interaction):
    assert interaction.guild is not None
    gp = get_player(interaction.guild.id)
    if gp.voice and gp.voice.is_playing():
        gp.voice.pause()
        await interaction.response.send_message("⏸️ Paused.")
    else:
        await interaction.response.send_message("Nothing is playing.", ephemeral=True)

@bot.tree.command(name="resume", description="Resume the song")
async def resume(interaction: discord.Interaction):
    assert interaction.guild is not None
    gp = get_player(interaction.guild.id)
    if gp.voice and gp.voice.is_paused():
        gp.voice.resume()
        await interaction.response.send_message("▶️ Lanjut.")
    else:
        await interaction.response.send_message("Nothing is paused.", ephemeral=True)

@bot.tree.command(name="stop", description="Stop and clear the queue")
async def stop(interaction: discord.Interaction):
    assert interaction.guild is not None
    gp = get_player(interaction.guild.id)
    while not gp.queue.empty():
        try:
            gp.queue.get_nowait()
            gp.queue.task_done()
        except Exception:
            break
    if gp.voice and (gp.voice.is_playing() or gp.voice.is_paused()):
        gp.voice.stop()
    await update_presence(None)
    await interaction.response.send_message("⏹️ Stopped and cleared the queue.")

@bot.tree.command(name="clear", description="Clear all queued tracks without stopping the current song")
async def clear_cmd(interaction: discord.Interaction):
    assert interaction.guild is not None
    gp = get_player(interaction.guild.id)
    cleared = 0
    while not gp.queue.empty():
        try:
            gp.queue.get_nowait()
            gp.queue.task_done()
            cleared += 1
        except Exception:
            break
    await interaction.response.send_message(f"🧹 Queue cleared. Removed {cleared} tracks.")

@bot.tree.command(name="loop", description="Set or show loop mode (off/track/queue)")
@app_commands.describe(mode="Loop mode (off/track/queue)")
@app_commands.choices(mode=[
    app_commands.Choice(name="off", value="off"),
    app_commands.Choice(name="track", value="track"),
    app_commands.Choice(name="queue", value="queue"),
])
async def loop_cmd(interaction: discord.Interaction, mode: Optional[app_commands.Choice[str]] = None):
    assert interaction.guild is not None
    gp = get_player(interaction.guild.id)

    if mode is None:
        status = "track" if gp.loop_one else ("queue" if gp.loop_all else "off")
        await interaction.response.send_message(f"🔁 Loop mode: {status}.")
        return

    if mode.value == "off":
        gp.loop_one = False
        gp.loop_all = False
        msg = "🔁 Loop disabled."
    elif mode.value == "track":
        gp.loop_one = True
        gp.loop_all = False
        msg = "🔂 Looping current track."
    else:  # queue
        gp.loop_one = False
        gp.loop_all = True
        msg = "🔁 Looping the queue."

    await interaction.response.send_message(msg)

@bot.tree.command(name="remove", description="Remove one or a range of tracks from the queue")
@app_commands.describe(index="1-based position to remove", end="Optional end position (inclusive) to remove a range")
async def remove_cmd(interaction: discord.Interaction, index: int, end: Optional[int] = None):
    assert interaction.guild is not None
    gp = get_player(interaction.guild.id)

    dq = getattr(gp.queue, "_queue", None)
    if dq is None:
        await interaction.response.send_message("Queue is empty.", ephemeral=True)
        return

    items: List[Track] = list(dq)
    qlen = len(items)
    if qlen == 0:
        await interaction.response.send_message("Queue is empty.", ephemeral=True)
        return

    # Normalize indices
    start = index
    stop = end if end is not None else index
    try:
        start = int(start)
        stop = int(stop)
    except Exception:
        await interaction.response.send_message("Index must be integers.", ephemeral=True)
        return

    if start > stop:
        start, stop = stop, start

    if start < 1:
        start = 1
    if stop > qlen:
        stop = qlen

    if start > qlen:
        await interaction.response.send_message(f"Index out of range. Queue has {qlen} items.", ephemeral=True)
        return

    removed = items[start-1:stop]  # inclusive range
    kept = items[:start-1] + items[stop:]

    # Apply back to the underlying deque
    dq.clear()
    for it in kept:
        dq.append(it)

    if len(removed) == 1:
        r = removed[0]
        await interaction.response.send_message(
            f"🗑️ Removed **{r.title}** from the queue."
        )
    else:
        preview = "".join(f"- **[{t.title}]({t.webpage_url})**" for t in removed[:5])
        more = "" if len(removed) <= 5 else f"…and {len(removed)-5} more"
        await interaction.response.send_message(
            f"🗑️ Removed {len(removed)} tracks from the queue:{preview}{more}"
        )

@bot.tree.command(name="move", description="Move a track to a new position in the queue")
@app_commands.describe(src="1-based source position", dest="1-based destination position")
async def move_cmd(interaction: discord.Interaction, src: int, dest: int):
    assert interaction.guild is not None
    gp = get_player(interaction.guild.id)

    dq = getattr(gp.queue, "_queue", None)
    if dq is None:
        await interaction.response.send_message("Queue is empty.", ephemeral=True)
        return

    items: List[Track] = list(dq)
    qlen = len(items)
    if qlen == 0:
        await interaction.response.send_message("Queue is empty.", ephemeral=True)
        return

    try:
        src = int(src)
        dest = int(dest)
    except Exception:
        await interaction.response.send_message("Indexes must be integers.", ephemeral=True)
        return

    if src < 1 or src > qlen or dest < 1 or dest > qlen:
        await interaction.response.send_message(f"Index out of range. Queue has {qlen} items.", ephemeral=True)
        return

    if src == dest:
        await interaction.response.send_message("Source and destination are the same.", ephemeral=True)
        return

    item = items.pop(src - 1)
    items.insert(dest - 1, item)

    dq.clear()
    for it in items:
        dq.append(it)

    await interaction.response.send_message(f"↔️ Moved **{item.title}** from {src} to {dest}.")

@bot.tree.command(name="shuffle", description="Shuffle the queue")
async def shuffle_cmd(interaction: discord.Interaction):
    assert interaction.guild is not None
    gp = get_player(interaction.guild.id)

    dq = getattr(gp.queue, "_queue", None)
    if dq is None:
        await interaction.response.send_message("Queue is empty.", ephemeral=True)
        return

    items: List[Track] = list(dq)
    if len(items) < 2:
        await interaction.response.send_message("Not enough items to shuffle.", ephemeral=True)
        return

    random.shuffle(items)

    dq.clear()
    for it in items:
        dq.append(it)

    await interaction.response.send_message(f"🔀 Shuffled {len(items)} queued tracks.")

@bot.tree.command(name="np", description="Show the currently playing track")
async def np_cmd(interaction: discord.Interaction):
    assert interaction.guild is not None
    gp = get_player(interaction.guild.id)
    if gp.now_playing:
        t = gp.now_playing
        elapsed = 0
        if gp.play_started_at is not None:
            try:
                elapsed = max(0, int(time.monotonic() - gp.play_started_at))
            except Exception:
                elapsed = 0
        def fmt(s: int) -> str:
            h, rem = divmod(s, 3600)
            m, s = divmod(rem, 60)
            return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"
        ts = f" [{fmt(min(elapsed, t.duration))}/{fmt(t.duration)}]" if t.duration else f" [{fmt(elapsed)}]"
        await interaction.response.send_message(
            f"🎵 Now Playing: **[{t.title}]({t.webpage_url})**{ts} — requested by <@{t.requested_by_id}>"
        )
    else:
        await interaction.response.send_message("Nothing is playing right now.", ephemeral=True)

@bot.tree.command(name="nowplaying", description="Show the currently playing track")
async def nowplaying_cmd(interaction: discord.Interaction):
    assert interaction.guild is not None
    gp = get_player(interaction.guild.id)
    if gp.now_playing:
        t = gp.now_playing
        elapsed = 0
        if gp.play_started_at is not None:
            try:
                elapsed = max(0, int(time.monotonic() - gp.play_started_at))
            except Exception:
                elapsed = 0
        def fmt(s: int) -> str:
            h, rem = divmod(s, 3600)
            m, s = divmod(rem, 60)
            return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"
        ts = f" [{fmt(min(elapsed, t.duration))}/{fmt(t.duration)}]" if t.duration else f" [{fmt(elapsed)}]"
        await interaction.response.send_message(
            f"🎵 Now Playing: **[{t.title}]({t.webpage_url})**{ts} — requested by <@{t.requested_by_id}>"
        )
    else:
        await interaction.response.send_message("Nothing is playing right now.", ephemeral=True)

# --- Queue pagination helpers ---
class QueuePaginator(discord.ui.View):
    def __init__(self, requester_id: int, now_playing: Optional[Track], play_started_at: Optional[float], pending: List[Track], per_page: int = 10, timeout: Optional[float] = 120):
        super().__init__(timeout=timeout)
        self.requester_id = requester_id
        self.now_playing = now_playing
        self.play_started_at = play_started_at
        self.pending = pending
        self.per_page = per_page
        self.page = 0
        self.total_pages = (len(self.pending) + self.per_page - 1) // self.per_page if self.pending else 1
        self._update_buttons()

    def _fmt(self, s: int) -> str:
        h, rem = divmod(s, 3600)
        m, s = divmod(rem, 60)
        return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"

    def _render(self) -> str:
        sections: List[str] = []
        # Now playing
        if self.now_playing:
            t = self.now_playing
            elapsed = 0
            if self.play_started_at is not None:
                try:
                    elapsed = max(0, int(time.monotonic() - self.play_started_at))
                except Exception:
                    elapsed = 0
            if t.duration:
                elapsed = min(elapsed, t.duration)
                ts = f"[{self._fmt(elapsed)}/{self._fmt(t.duration)}]"
            else:
                ts = f"[{self._fmt(elapsed)}]"
            sections.append(f"🎵 Now Playing: **{t.title}** {ts} — requested by <@{t.requested_by_id}>\n")

        if not self.pending:
            sections.append("Up next: (empty)")
            return "".join(sections)

        start = self.page * self.per_page
        end = start + self.per_page
        slice_ = self.pending[start:end]

        up_next_lines = [f"**Up next** (page {self.page + 1}/{self.total_pages}):"]
        for idx, it in enumerate(slice_, start=start + 1):
            up_next_lines.append(f"\n{idx}. **{it.title}** — requested by <@{it.requested_by_id}>")
        if end < len(self.pending):
            up_next_lines.append(f"\n…{len(self.pending) - end} more queued")
        sections.append("".join(up_next_lines))
        return "".join(sections)

    def _update_buttons(self):
        # Ensure buttons are in correct enabled/disabled state
        for child in self.children:
            if isinstance(child, discord.ui.Button):
                if child.custom_id == "prev":
                    child.disabled = (self.page <= 0)
                elif child.custom_id == "next":
                    child.disabled = (self.page >= self.total_pages - 1)

    async def _maybe_block(self, interaction: discord.Interaction) -> bool:
        # Only allow the original requester to control pagination
        if interaction.user.id != self.requester_id:
            try:
                await interaction.response.send_message("You can't control this paginator.", ephemeral=True)
            except Exception:
                pass
            return True
        return False

    @discord.ui.button(label="Previous", style=discord.ButtonStyle.secondary, custom_id="prev")
    async def previous(self, interaction: discord.Interaction, button: discord.ui.Button):
        if await self._maybe_block(interaction):
            return
        if self.page > 0:
            self.page -= 1
        self._update_buttons()
        await interaction.response.edit_message(content=self._render(), view=self)

    @discord.ui.button(label="Next", style=discord.ButtonStyle.primary, custom_id="next")
    async def next(self, interaction: discord.Interaction, button: discord.ui.Button):
        if await self._maybe_block(interaction):
            return
        if self.page < self.total_pages - 1:
            self.page += 1
        self._update_buttons()
        await interaction.response.edit_message(content=self._render(), view=self)

@bot.tree.command(name="queue", description="Show the upcoming queue")
async def queue_cmd(interaction: discord.Interaction):
    assert interaction.guild is not None
    gp = get_player(interaction.guild.id)

    # Snapshot pending items from the internal deque without consuming the queue
    try:
        dq = list(getattr(gp.queue, "_queue", []))
        pending: List[Track] = list(dq)
    except Exception:
        pending = []

    if not pending and not gp.now_playing:
        await interaction.response.send_message("Queue is empty.", ephemeral=True)
        return

    # If few items, just print without paginator
    if len(pending) <= 10:
        sections: List[str] = []
        # Now Playing (with timestamp)
        if gp.now_playing:
            t = gp.now_playing
            elapsed = 0
            if gp.play_started_at is not None:
                try:
                    elapsed = max(0, int(time.monotonic() - gp.play_started_at))
                except Exception:
                    elapsed = 0
            def fmt(s: int) -> str:
                h, rem = divmod(s, 3600)
                m, s = divmod(rem, 60)
                return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"
            ts = f"[{fmt(min(elapsed, t.duration))}/{fmt(t.duration)}]" if t.duration else f"[{fmt(elapsed)}]"
            sections.append(f"🎵 Now Playing: **{t.title}** {ts} — requested by <@{t.requested_by_id}>\n")

        if pending:
            up_next_lines = ["**Up next:**"]
            for idx, it in enumerate(pending[:10], start=1):
                up_next_lines.append(f"\n{idx}. **{it.title}** — requested by <@{it.requested_by_id}>")
            sections.append("".join(up_next_lines))
        else:
            sections.append("Up next: (empty)")

        await interaction.response.send_message("".join(sections))
        return

    # Many items: use paginator view
    view = QueuePaginator(requester_id=interaction.user.id, now_playing=gp.now_playing, play_started_at=gp.play_started_at, pending=pending, per_page=10, timeout=180)
    await interaction.response.send_message(view._render(), view=view)

@bot.tree.command(name="leave", description="Disconnect the bot from voice")
async def leave(interaction: discord.Interaction):
    assert interaction.guild is not None
    gp = get_player(interaction.guild.id)
    if gp.voice and gp.voice.is_connected():
        await gp.voice.disconnect(force=True)
        gp.voice = None
        await update_presence(None)
        await interaction.response.send_message("👋 Left voice.")
    else:
        await interaction.response.send_message("I'm not in a voice channel.", ephemeral=True)


# ------------- Entrypoint -------------
async def main() -> None:
    if not DISCORD_TOKEN:
        raise SystemExit("DISCORD_TOKEN belum diset. Buat file .env dan isi DISCORD_TOKEN=...")
    async with bot:
        await bot.start(DISCORD_TOKEN)


if __name__ == "__main__":
    # Python 3.10+ recommended. For 3.12, do not use get_event_loop(); use asyncio.run.
    asyncio.run(main())
