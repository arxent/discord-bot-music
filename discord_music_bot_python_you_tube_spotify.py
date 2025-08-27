import os
import re
import asyncio
from dataclasses import dataclass, field
from typing import Optional, List, Tuple

import discord
from discord import app_commands
from discord.ext import commands

# --- Third-party helpers ---
# Ensure you installed: pip install -U discord.py[voice] yt-dlp spotipy python-dotenv
import yt_dlp
from dotenv import load_dotenv

try:
    import spotipy
    from spotipy.oauth2 import SpotifyClientCredentials
    SPOTIFY_AVAILABLE = True
except Exception:
    SPOTIFY_AVAILABLE = False

# ------------- Config -------------
load_dotenv()
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
FFMPEG_PATH = os.getenv("FFMPEG_PATH", "ffmpeg")

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
    requested_by: str

@dataclass
class GuildPlayer:
    voice: Optional[discord.VoiceClient] = None
    queue: asyncio.Queue = field(default_factory=asyncio.Queue)
    now_playing: Optional[Track] = None
    player_task: Optional[asyncio.Task] = None
    volume: float = 0.5
    announce_channel_id: Optional[int] = None

    async def ensure_player(self, bot: commands.Bot, guild: discord.Guild):
        if self.player_task is None or self.player_task.done():
            self.player_task = asyncio.create_task(self._player_loop(bot, guild))

    async def _player_loop(self, bot: commands.Bot, guild: discord.Guild):
        while True:
            track: Track = await self.queue.get()
            self.now_playing = track
            if not self.voice or not self.voice.is_connected():
                self.now_playing = None
                continue

            # Update presence
            await update_presence(track.title)

            # Announce now playing in the last command channel (if known)
            try:
                if self.announce_channel_id:
                    ch = bot.get_channel(self.announce_channel_id)
                    if ch:
                        await ch.send(f"🎶 Now Playing: **[{track.title}]({track.webpage_url})**")
            except Exception:
                pass

            def after_playback(_):
                bot.loop.call_soon_threadsafe(next_event.set)

            next_event = asyncio.Event()
            try:
                source = discord.FFmpegPCMAudio(track.url, executable=FFMPEG_PATH, **FFMPEG_OPTS)
                audio = discord.PCMVolumeTransformer(source, volume=self.volume)
                self.voice.play(audio, after=after_playback)
                # Announce now playing in a text channel
                try:
                    default_channel = guild.system_channel or discord.utils.get(guild.text_channels, permissions__send_messages=True)
                    if default_channel:
                        asyncio.create_task(default_channel.send(f"🎶 Now playing: **{track.title}** — diminta oleh {track.requested_by}
{track.webpage_url}"))
                except Exception:
                    pass
            except Exception as e:
                print(f"Playback error: {e} — mencoba re-ekstrak dengan klien lain…")
                try:
                    title, stream, page = await extract_from_youtube(track.webpage_url)
                    track.title, track.url, track.webpage_url = title, stream, page
                    source = discord.FFmpegPCMAudio(track.url, executable=FFMPEG_PATH, **FFMPEG_OPTS)
                    audio = discord.PCMVolumeTransformer(source, volume=self.volume)
                    self.voice.play(audio, after=after_playback)
                except Exception as ee:
                    print(f"Fallback playback failed: {ee}")
                    next_event.set()

            await next_event.wait()
            self.now_playing = None
            if self.queue.empty():
                await update_presence(None)

# ------------- Helpers -------------
YOUTUBE_URL_RE = re.compile(r"(https?://)?(www\.)?(youtube\.com|youtu\.be)/.+")
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

async def extract_from_youtube(query: str) -> Tuple[str, str, str]:
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
                    audio_fmts = [f for f in fmts if f.get("vcodec") in ("none", None) and f.get("acodec") not in ("none", None) and f.get("url")]
                    def _key(f):
                        ext = f.get("ext") or ""
                        abr = f.get("abr") or 0
                        return (ext != "m4a", -abr)
                    audio_fmts.sort(key=_key)
                    if audio_fmts:
                        stream_url = audio_fmts[0]["url"]
                return info.get("title", "Unknown"), stream_url, info.get("webpage_url", info.get("original_url", query))

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

    title, stream_url, webpage_url = await _try_with_clients(["android", "web", "tv"])
    if not stream_url:
        raise RuntimeError("Tidak dapat mengekstrak audio dari YouTube.")
    return title, stream_url, webpage_url

async def resolve_spotify_to_query(url: str) -> List[str]:
    sp = get_spotify_client()
    if sp is None:
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

async def make_track(query_or_url: str, requested_by: str) -> Track:
    if SPOTIFY_URL_RE.match(query_or_url):
        queries = await resolve_spotify_to_query(query_or_url)
        first = queries[0]
        title, stream, page = await extract_from_youtube(first)
        return Track(title=title, url=stream, webpage_url=page, requested_by=requested_by)
    else:
        title, stream, page = await extract_from_youtube(query_or_url)
        return Track(title=title, url=stream, webpage_url=page, requested_by=requested_by)

# ------------- Bot setup -------------
intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix=commands.when_mentioned_or("!"), intents=intents)

guild_players: dict[int, GuildPlayer] = {}

# Presence helper
async def update_presence(title: Optional[str]):
    try:
        if title:
            await bot.change_presence(activity=discord.Activity(type=discord.ActivityType.listening, name=title))
        else:
            await bot.change_presence(activity=None)
    except Exception:
        pass

def get_player(guild_id: int) -> GuildPlayer:
    gp = guild_players.get(guild_id)
    if gp is None:
        gp = GuildPlayer()
        guild_players[guild_id] = gp
    return gp

async def ensure_voice(interaction: discord.Interaction) -> GuildPlayer:
    assert interaction.guild is not None
    gp = get_player(interaction.guild.id)

    if gp.voice and gp.voice.is_connected():
        return gp

    if not interaction.user or not isinstance(interaction.user, discord.Member):
        raise RuntimeError("Tidak dapat menemukan channel suara kamu.")

    if not interaction.user.voice or not interaction.user.voice.channel:
        raise RuntimeError("Kamu harus bergabung ke voice channel terlebih dahulu.")

    channel = interaction.user.voice.channel
    gp.voice = await channel.connect()
    return gp

# ------------- Slash commands -------------
@bot.event
async def on_ready():
    try:
        if GUILD_ID:
            guild = discord.Object(id=GUILD_ID)
            bot.tree.copy_global_to(guild=guild)
            synced = await bot.tree.sync(guild=guild)
            print(f"Synced {len(synced)} commands to guild {GUILD_ID}")
        else:
            synced = await bot.tree.sync()
            print(f"Synced {len(synced)} global commands")
    except Exception as e:
        print(f"Slash sync failed: {e}")
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")

@bot.tree.command(name="ping", description="Cek apakah bot responsif")
async def ping(interaction: discord.Interaction):
    await interaction.response.send_message("Pong! ✅")

@bot.tree.command(name="join", description="Bot join ke voice channel kamu")
async def join(interaction: discord.Interaction):
    try:
        await interaction.response.defer(ephemeral=True)
        gp = await ensure_voice(interaction)
        await interaction.followup.send(f"✅ Bergabung di: {gp.voice.channel}", ephemeral=True)
    except Exception as e:
        await interaction.response.send_message(f"❌ {e}", ephemeral=True)

@bot.tree.command(name="play", description="Putar lagu dari YouTube atau Spotify URL / judul lagu")
@app_commands.describe(query="Judul lagu, link YouTube, atau link Spotify")
async def play(interaction: discord.Interaction, query: str):
    assert interaction.guild is not None
    await interaction.response.defer()
    try:
        gp = await ensure_voice(interaction)
        # remember channel for now-playing announcements
        gp.announce_channel_id = interaction.channel.id if interaction.channel else None
        track = await make_track(query, requested_by=interaction.user.display_name)
        await gp.queue.put(track)
        await gp.ensure_player(bot, interaction.guild)
        await interaction.followup.send(f"✅ Ditambahkan: **[{track.title}]({track.webpage_url})** — diminta oleh {track.requested_by}")
    except Exception as e:
        await interaction.followup.send(f"❌ Gagal menambah lagu: {e}")

@bot.tree.command(name="spotify", description="Putar dari Spotify (URL track/playlist atau judul di Spotify)")
@app_commands.describe(query="Contoh: link Spotify atau 'Someone Like You Adele'")
async def spotify_cmd(interaction: discord.Interaction, query: str):
    assert interaction.guild is not None
    await interaction.response.defer()
    try:
        gp = await ensure_voice(interaction)
        # remember channel for now-playing announcements
        gp.announce_channel_id = interaction.channel.id if interaction.channel else None
        if SPOTIFY_URL_RE.match(query):
            queries = await resolve_spotify_to_query(query)
            titles_links: List[Tuple[str, str]] = []
            for q in queries[:50]:
                t = await make_track(q, requested_by=interaction.user.display_name)
                await gp.queue.put(t)
                titles_links.append((t.title, t.webpage_url))
            await gp.ensure_player(bot, interaction.guild)
            added = len(titles_links)
            if added:
                preview = "
".join(f"- [{title}]({url})" for title, url in titles_links[:5])
                more = "" if added <= 5 else f"
…dan {added-5} lagi"
                msg = f"✅ Ditambahkan {added} lagu dari Spotify:
{preview}{more}"
            else:
                msg = "❌ Tidak ada lagu yang dapat ditambahkan."
            await interaction.followup.send(msg)
            return
        sp = get_spotify_client()
        if sp is None:
            await interaction.followup.send("❌ Spotify belum dikonfigurasi. Set SPOTIFY_CLIENT_ID/SECRET di .env.")
            return
        res = sp.search(q=query, type="track", limit=1)
        items = res.get("tracks", {}).get("items", [])
        if not items:
            await interaction.followup.send("❌ Tidak ditemukan di Spotify.")
            return
        tt = items[0]
        name = tt.get("name")
        artists = ", ".join(a.get("name") for a in tt.get("artists", []))
        yquery = f"{name} {artists} audio"
        t = await make_track(yquery, requested_by=interaction.user.display_name)
        await gp.queue.put(t)
        await gp.ensure_player(bot, interaction.guild)
        await interaction.followup.send(f"✅ Ditambahkan dari Spotify: **[{name} — {artists}]({t.webpage_url})**")
    except Exception as e:
        await interaction.followup.send(f"❌ Gagal memproses Spotify: {e}")

@bot.tree.command(name="skip", description="Lewati lagu yang sedang diputar")
async def skip(interaction: discord.Interaction):
    assert interaction.guild is not None
    gp = get_player(interaction.guild.id)
    if gp.voice and gp.voice.is_playing():
        gp.voice.stop()
        await interaction.response.send_message("⏭️ Dilewati.")
    else:
        await interaction.response.send_message("Tidak ada lagu yang diputar.", ephemeral=True)

@bot.tree.command(name="np", description="Tampilkan lagu yang sedang diputar")
async def now_playing(interaction: discord.Interaction):
    assert interaction.guild is not None
    gp = get_player(interaction.guild.id)
    if gp.now_playing:
        t = gp.now_playing
        await interaction.response.send_message(f"🎶 Sedang diputar: **{t.title}** — diminta oleh {t.requested_by}
{t.webpage_url}")
    else:
        await interaction.response.send_message("Tidak ada lagu yang sedang diputar.", ephemeral=True)


@bot.tree.command(name="pause", description="Jeda lagu")
async def pause(interaction: discord.Interaction):
    assert interaction.guild is not None
    gp = get_player(interaction.guild.id)
    if gp.voice and gp.voice.is_playing():
        gp.voice.pause()
        await interaction.response.send_message("⏸️ Dijeda.")
    else:
        await interaction.response.send_message("Tidak ada lagu yang diputar.", ephemeral=True)

@bot.tree.command(name="resume", description="Lanjutkan lagu")
async def resume(interaction: discord.Interaction):
    assert interaction.guild is not None
    gp = get_player(interaction.guild.id)
    if gp.voice and gp.voice.is_paused():
        gp.voice.resume()
        await interaction.response.send_message("▶️ Lanjut.")
    else:
        await interaction.response.send_message("Tidak ada lagu yang dijeda.", ephemeral=True)

@bot.tree.command(name="stop", description="Hentikan dan kosongkan antrian")
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
    await interaction.response.send_message("⏹️ Dihentikan dan antrian dikosongkan.")

@bot.tree.command(name="clear", description="Kosongkan semua queue tanpa menghentikan musik yang sedang dimainkan")
async def clear(interaction: discord.Interaction):
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
    await interaction.response.send_message(f"🧹 Antrian dibersihkan. {cleared} lagu dihapus.")

@bot.tree.command(name="leave", description="Bot keluar dari voice")
async def leave(interaction: discord.Interaction):
    assert interaction.guild is not None
    gp = get_player(interaction.guild.id)
    if gp.voice and gp.voice.is_connected():
        await gp.voice.disconnect(force=True)
        gp.voice = None
        await update_presence(None)
        await interaction.response.send_message("👋 Keluar dari voice.")
    else:
        await interaction.response.send_message("Aku tidak berada di voice.", ephemeral=True)


# Now Playing commands
@bot.tree.command(name="np", description="Tampilkan lagu yang sedang diputar")
@bot.tree.command(name="nowplaying", description="Tampilkan lagu yang sedang diputar")
async def now_playing_cmd(interaction: discord.Interaction):
    assert interaction.guild is not None
    gp = get_player(interaction.guild.id)
    if gp.now_playing:
        t = gp.now_playing
        await interaction.response.send_message(f"🎵 Now Playing: **[{t.title}]({t.webpage_url})** — diminta oleh {t.requested_by}")
    else:
        await interaction.response.send_message("Tidak ada yang diputar saat ini.", ephemeral=True)


async def main():
    if not DISCORD_TOKEN:
        raise SystemExit("DISCORD_TOKEN belum diset. Buat file .env dan isi DISCORD_TOKEN=...")
    async with bot:
        await bot.start(DISCORD_TOKEN)


if __name__ == "__main__":
    asyncio.run(main())
