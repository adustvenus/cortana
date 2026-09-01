"""Media-tool tests.

Three processes now share ONE Spotify grant and one rate limit: the Electron
dashboard, the phone bridge, and this voice tool. Almost every failure that
matters here is invisible from the outside - a cool-off written in the wrong
unit silences nobody, a cool-off that overwrites a longer one releases the other
two early, a refresh that retries forever burns the quota it is trying to
protect, and every missing-binary path on the runtime box has to end in a spoken
sentence rather than a traceback the user hears as silence.

Every test below is one of those. None of them may touch the network: media._http
is the single seam and the fixture replaces it with something that fails loudly
if a test reaches it when it should not.
"""
import json
import time

import pytest

from tools import media


class Resp:
    """Just enough of requests.Response for this module's getattr checks."""

    def __init__(self, status=200, body=None, headers=None, text=""):
        self.status_code = status
        self.ok = 200 <= status < 300
        self.headers = headers or {}
        self._body = body
        self.text = text

    def json(self):
        if self._body is None:
            raise ValueError("no json")
        return self._body


class Net:
    """Records every HTTP attempt and answers from a route table.

    Default answer is an explicit failure: a test that reaches an endpoint it
    did not plan for should say so, not quietly get a 200."""

    def __init__(self):
        self.calls = []
        self.routes = []

    def add(self, needle, resp):
        self.routes.append((needle, resp))

    def __call__(self, method, url, **kw):
        self.calls.append((method, url, kw))
        # LONGEST needle wins, not first-registered. Plain first-match made
        # "/me/player" silently shadow "/me/player/play" and "/me/player/devices",
        # so a test stubbed a 403 and quietly got a 200 from an earlier route -
        # it passed while asserting nothing, twice, before anyone noticed.
        # Registration order is not something a test should have to reason about.
        for needle, resp in sorted(self.routes, key=lambda r: -len(r[0])):
            if needle in url:
                return resp(method, url, kw) if callable(resp) else resp
        return Resp(500, text="unrouted " + url)

    def paths(self):
        return [u for _, u, _ in self.calls]


@pytest.fixture
def net(monkeypatch):
    n = Net()
    monkeypatch.setattr(media, "_http", n)
    return n


@pytest.fixture
def files(tmp_path, monkeypatch, net):
    """Point the three shared files at a throwaway directory.

    They are read and written by the Electron shell and the bridge for real; a
    test that wrote the actual spotify_token.json would log the user out."""
    monkeypatch.setattr(media, "CONFIG_FILE", tmp_path / "spotify.json")
    monkeypatch.setattr(media, "TOKEN_FILE", tmp_path / "spotify_token.json")
    monkeypatch.setattr(media, "BACKOFF_FILE", tmp_path / "spotify_backoff.json")
    media.CONFIG_FILE.write_text(json.dumps({"clientId": "abc123"}))
    media._device.update(id=None, until=0.0)
    return tmp_path


def write_token(**over):
    t = {"access_token": "AT", "refresh_token": "RT", "scope": "s",
         "expires_at": int(time.time() * 1000) + 3600_000}
    t.update(over)
    media.TOKEN_FILE.write_text(json.dumps(t))
    return t


def no_binaries(monkeypatch, present=()):
    """Model the runtime box, where playerctl is simply not installed."""
    monkeypatch.setattr(media.shutil, "which",
                        lambda name: ("/usr/bin/" + name) if name in present else None)


def fake_run(monkeypatch, rc=0, out=""):
    calls = []

    def run(args, **kw):
        calls.append(list(args))

        class P:
            returncode = rc
            stdout = out
            stderr = ""
        return P()
    monkeypatch.setattr(media.subprocess, "run", run)
    return calls


# -- the shared cool-off ----------------------------------------------------
@pytest.mark.parametrize("action,query", [("play", ""), ("status", ""),
                                          ("next", ""), ("previous", ""),
                                          ("pause", ""), ("play_query", "cello")])
def test_a_running_cooloff_spends_no_request(files, net, monkeypatch, action, query):
    """A 429 earned by the dashboard or the bridge must silence THIS process too.

    The whole point of the shared file is that the third client stops asking.
    If any action still reaches the network while the cool-off runs, the voice
    tool becomes the thing that keeps the account rate limited.
    """
    write_token()
    media.BACKOFF_FILE.write_text(json.dumps({"until": time.time() + 30,
                                              "reason": "429 from Spotify",
                                              "at": time.time()}))
    no_binaries(monkeypatch)
    said = media.media(action, query)
    assert net.calls == [], f"{action} hit the network during a cool-off"
    assert "rate limited" in said


def test_volume_still_works_while_spotify_is_cooling_off(files, net, monkeypatch):
    """Volume goes to the system sink, so it must not be hostage to Spotify's
    quota - "turn it down" is the one media request that has to work always."""
    media.BACKOFF_FILE.write_text(json.dumps({"until": time.time() + 300}))
    no_binaries(monkeypatch, present=("pactl",))
    calls = fake_run(monkeypatch)
    said = media.media("volume", percent=40)
    assert net.calls == []
    assert "40" in said
    assert ["pactl", "set-sink-volume", "@DEFAULT_SINK@", "40%"] in calls


def test_a_429_writes_the_cooloff_in_the_format_the_other_two_read(files, net, monkeypatch):
    """`until` is epoch SECONDS, not milliseconds.

    spotify.js does `Date.now()/1000` and spotify_link.py does `time.time()`.
    Writing millis here would park the cool-off in the year 56000 and silence
    all three processes permanently; writing a duration instead of a deadline
    would silence nobody. Neither shows up as an error anywhere.
    """
    write_token()
    # "play" now reads /me/player first, to tell "paused, resume it" from
    # "idle, wake the speaker". Stubbed 204 so the flow reaches the endpoint
    # this test is about. Order matters: Net matches by substring and takes the
    # FIRST hit, so "/me/player" would otherwise shadow "/me/player/devices".
    net.add("/me/player/devices", Resp(429, headers={"Retry-After": "17"}))
    net.add("/me/player", Resp(204))
    no_binaries(monkeypatch)
    said = media.media("play")

    d = json.loads(media.BACKOFF_FILE.read_text())
    assert set(d) == {"until", "reason", "at"}
    assert 15 <= d["until"] - time.time() <= 19
    assert abs(d["at"] - time.time()) < 5
    assert "17 seconds" in said


def test_a_missing_retry_after_still_backs_off(files, net, monkeypatch):
    write_token()
    net.add("/me/player/devices", Resp(429))   # specific route first, see above
    net.add("/me/player", Resp(204))
    no_binaries(monkeypatch)
    media.media("play")
    assert 25 <= json.loads(media.BACKOFF_FILE.read_text())["until"] - time.time() <= 31


def test_a_short_cooloff_never_shortens_a_longer_one(files):
    """A Retry-After of 5 arriving while a 300s cool-off runs must not release
    the other two processes early. They overwrite unconditionally; this side
    only ever extends, so whichever of the three is most pessimistic wins."""
    long_until = time.time() + 300
    media.BACKOFF_FILE.write_text(json.dumps({"until": long_until, "reason": "x",
                                              "at": time.time()}))
    media._set_backoff(5, "shorter")
    assert json.loads(media.BACKOFF_FILE.read_text())["until"] == long_until
    media._set_backoff(900, "longer")
    assert json.loads(media.BACKOFF_FILE.read_text())["until"] > long_until


# -- setup that has not happened yet ----------------------------------------
def test_a_missing_token_file_is_a_sentence_not_a_traceback(files, net, monkeypatch):
    """spotify_token.json is gitignored, so it is absent on a fresh box and on
    every clone. Reaching this path through the voice tool must produce
    something speakable - an exception here is heard as dead air."""
    no_binaries(monkeypatch)
    said = media.media("status")
    assert isinstance(said, str) and said
    assert "connect" in said.lower()
    assert net.calls == []


def test_an_unconfigured_client_id_says_which_step_is_missing(files, net, monkeypatch):
    media.CONFIG_FILE.write_text(json.dumps({"clientId": "YOUR_CLIENT_ID"}))
    write_token()
    no_binaries(monkeypatch)
    said = media.media("next")
    assert "client ID" in said
    assert net.calls == []


def test_a_missing_playerctl_degrades_to_a_sentence(files, net, monkeypatch):
    """playerctl is NOT installed on the runtime box, so every non-Spotify path
    ends here. It must name the binary - "nothing happened" is unfixable."""
    no_binaries(monkeypatch)          # no token either, so pause falls through
    said = media.media("pause")
    assert "playerctl" in said
    assert net.calls == []


def test_a_transport_failure_is_a_sentence(files, net, monkeypatch):
    def boom(method, url, **kw):
        raise OSError("Connection refused")
    monkeypatch.setattr(media, "_http", boom)
    write_token()
    no_binaries(monkeypatch)
    said = media.media("next")
    assert "couldn't reach Spotify" in said


# -- the action router ------------------------------------------------------
@pytest.mark.parametrize("bogus", ["explode", "", "shuffle", "delete_playlist",
                                   None, "playnext"])
def test_an_unknown_action_is_refused_and_lists_the_real_ones(files, net, bogus):
    """The model will invent verbs. An unknown one must never fall through to
    play - "shuffle" silently starting playback is worse than a refusal."""
    said = media.media(bogus)
    assert "I can play, pause" in said
    assert net.calls == []


def _route_everything(net):
    """Answer every endpoint this module can reach. Most specific needle first -
    Net matches on substring, in insertion order, so a bare "/me/player" added
    early would swallow "/me/player/play"."""
    net.add("/me/player/devices",
            Resp(200, body={"devices": [{"id": "LOCAL", "name": "Cortana"}]}))
    net.add("/search", Resp(200, body={"tracks": {"items": [
        {"uri": "spotify:track:1", "name": "Nightcall", "artists": []}]}}))
    for ep in ("/me/player/play", "/me/player/pause",
               "/me/player/next", "/me/player/previous"):
        net.add(ep, Resp(204))
    net.add("/me/player", Resp(200, body={
        "is_playing": True, "item": {"name": "Teardrop", "artists": []},
        "device": {"name": "Cortana", "id": "LOCAL"}}))


@pytest.mark.parametrize("spoken,canonical,query", [
    ("skip", "next", ""), ("Prev", "previous", ""), ("resume", "play", ""),
    ("stop", "pause", ""), ("now playing", "status", ""),
    ("search", "play_query", "nightcall")])
def test_the_obvious_synonyms_reach_the_canonical_action(files, net, monkeypatch,
                                                         spoken, canonical, query):
    """A synonym has to land on the RIGHT action, not merely on some action.

    The previous version of this test asserted only that the synonym differed
    from the refusal sentence, which would have passed just as happily if
    "skip" routed to pause or "stop" routed to play - the two mistakes most
    worth catching, since both are silent and both are heard as Cortana
    doing something other than what was asked.
    """
    _route_everything(net)
    no_binaries(monkeypatch)
    write_token()

    def run(verb):
        net.calls.clear()
        media._device.update(id=None, until=0.0)
        said = media.media(verb, query)
        return said, [u.split("/v1")[-1] for u in net.paths()]

    said_alias, paths_alias = run(spoken)
    said_canon, paths_canon = run(canonical)
    assert paths_alias == paths_canon, f"{spoken} did not act like {canonical}"
    assert said_alias == said_canon
    assert "I can play, pause" not in said_alias, f"{spoken} was refused"


# -- the refresh-token rotation race ----------------------------------------
def test_a_lost_rotation_race_retries_once_with_the_other_process_token(files, net):
    """The dashboard refreshed between our read and our POST, so the token we
    sent is already dead. Their newer pair is on disk - re-read once and use it.
    Without this, whichever process refreshes second logs itself out."""
    write_token(expires_at=int(time.time() * 1000) - 1000)   # forces a refresh
    posts = []

    def token_ep(method, url, kw):
        posts.append(kw["data"]["refresh_token"])
        if kw["data"]["refresh_token"] == "RT":
            # Meanwhile the dashboard wrote a fresh pair.
            write_token(refresh_token="RT2",
                        expires_at=int(time.time() * 1000) - 1000)
            return Resp(400, body={"error": "invalid_grant"})
        return Resp(200, body={"access_token": "AT2", "expires_in": 3600})

    net.add("accounts.spotify.com", token_ep)
    net.add("/me/player/devices", Resp(200, body={"devices": []}))
    net.add("/me/player", Resp(204))
    net.add("/me/player/play", Resp(204))
    # No device and nothing active: the honest answer, not a false "Playing."
    assert "nothing" in media.media("play").lower()
    assert posts == ["RT", "RT2"], "did not retry with the token the other side wrote"


def test_a_dead_grant_does_not_retry_forever(files, net, monkeypatch):
    """When the refresh token on disk is UNCHANGED, the rejection is real and
    not a race. Retrying then just spends more of a quota three processes share
    and turns one dead grant into a request storm."""
    write_token(expires_at=int(time.time() * 1000) - 1000)
    posts = []

    def token_ep(method, url, kw):
        posts.append(kw["data"]["refresh_token"])
        return Resp(400, body={"error": "invalid_grant"})

    net.add("accounts.spotify.com", token_ep)
    no_binaries(monkeypatch)
    said = media.media("next")
    assert len(posts) == 1
    assert "onnect" in said


def test_a_refresh_writes_the_pair_back_for_the_other_processes(files, net):
    """A refresh response usually omits the refresh token and the scope. Writing
    the response back verbatim drops both, which logs everyone out on the next
    rotation and blanks the scope the dashboard uses to tell a scope shortfall
    from a Premium 403."""
    write_token(expires_at=int(time.time() * 1000) - 1000)
    net.add("accounts.spotify.com", Resp(200, body={"access_token": "AT2",
                                                    "expires_in": 3600}))
    net.add("/me/player/devices", Resp(200, body={"devices": []}))
    net.add("/me/player/play", Resp(204))
    media.media("play")
    t = json.loads(media.TOKEN_FILE.read_text())
    assert t["access_token"] == "AT2"
    assert t["refresh_token"] == "RT"
    assert t["scope"] == "s"
    assert t["expires_at"] > time.time() * 1000


# -- pressing the right device ----------------------------------------------
def test_a_press_is_aimed_at_the_local_device_not_the_phone(files, net):
    """Without device_id the Web API acts on whatever Spotify last thought was
    active, which on this account is the phone. Saying "play" at the desk
    started music in the user's pocket."""
    write_token()
    net.add("/me/player/devices",
            Resp(200, body={"devices": [{"id": "PHONE", "name": "Pixel"},
                                        {"id": "LOCAL", "name": "Cortana"}]}))
    net.add("/me/player/next", Resp(204))
    assert media.media("next") == "Skipped forward."
    press = [c for c in net.calls if c[1].endswith("/me/player/next")][0]
    assert press[2]["params"] == {"device_id": "LOCAL"}


def test_no_local_device_still_presses(files, net):
    """spotifyd may be stopped or never installed. Falling back to Spotify's own
    idea of the active device keeps the tool working instead of failing closed."""
    write_token()
    net.add("/me/player/devices", Resp(200, body={"devices": []}))
    net.add("/me/player/next", Resp(204))
    assert media.media("next") == "Skipped forward."
    press = [c for c in net.calls if c[1].endswith("/me/player/next")][0]
    assert press[2]["params"] == {}


def test_a_404_press_forgets_the_cached_device(files, net, monkeypatch):
    """The cached id outlives spotifyd across a restart or a suspend. Keeping it
    means every press for a full TTL aims at an endpoint that no longer exists."""
    write_token()
    net.add("/me/player/devices",
            Resp(200, body={"devices": [{"id": "LOCAL", "name": "Cortana"}]}))
    net.add("/me/player/next", Resp(404))
    no_binaries(monkeypatch)
    said = media.media("next")
    assert "spotifyd" in said
    assert media._device["id"] is None


# -- pause means pause whatever is playing ----------------------------------
def test_pause_presses_spotify_only_when_spotify_is_the_thing_playing(files, net):
    write_token()
    net.add("/me/player", Resp(200, body={"is_playing": True,
                                          "item": {"name": "Weightless",
                                                   "artists": [{"name": "Marconi Union"}]},
                                          "device": {"name": "Cortana"}}))
    net.add("/me/player/devices",
            Resp(200, body={"devices": [{"id": "LOCAL", "name": "Cortana"}]}))
    net.add("/me/player/pause", Resp(204))
    assert media.media("pause") == "Paused."


def test_pause_falls_through_to_the_local_player_when_spotify_is_idle(files, net, monkeypatch):
    """"Pause the music" while YouTube is the thing talking must not press
    Spotify's already-paused pause button and report success into a room that
    is still making noise."""
    write_token()
    net.add("/me/player", Resp(204))
    no_binaries(monkeypatch, present=("playerctl",))
    calls = fake_run(monkeypatch)
    assert media.media("pause") == "Paused."
    # A `playerctl status` probe now runs first (the local-first fast path), so
    # assert on WHAT was pressed rather than on the exact call list - the latter
    # was really testing the implementation, not the behaviour.
    assert ["playerctl", "pause"] in calls
    assert not any("/me/player/pause" in u for u in net.paths())


def test_a_playing_local_player_is_paused_without_touching_spotify(files, net, monkeypatch):
    """The speed fix, asserted as behaviour: when a browser tab is the thing
    making noise, the press is one local D-Bus call and Spotify is never
    contacted at all - no token read, no /me/player, nothing.

    Before this, every pause cost 2-3 HTTPS round trips to api.spotify.com
    before she could speak, and pressed the wrong player besides."""
    write_token()
    no_binaries(monkeypatch, present=("playerctl",))
    calls = fake_run(monkeypatch, out="Playing")
    assert media.media("pause") == "Paused."
    assert ["playerctl", "pause"] in calls
    assert net.paths() == [], "reached Spotify when the local player was playing"


def test_naming_a_player_skips_detection_entirely(files, net, monkeypatch):
    """'pause youtube' must not consult Spotify, and 'pause spotify' must not
    consult playerctl - saying which one you mean is the whole point."""
    write_token()
    no_binaries(monkeypatch, present=("playerctl",))
    calls = fake_run(monkeypatch, out="Playing")
    assert media.media("pause", player="youtube") == "Paused."
    assert net.paths() == []

    net.add("/me/player/devices", Resp(200, body={"devices": []}))
    net.add("/me/player/pause", Resp(204))
    calls2 = fake_run(monkeypatch, out="Playing")
    assert media.media("pause", player="spotify") == "Paused."
    assert calls2 == [], "consulted playerctl after being told to use Spotify"
    assert any("/me/player/pause" in u for u in net.paths())


def test_pause_says_so_when_spotify_is_already_paused(files, net, monkeypatch):
    write_token()
    net.add("/me/player", Resp(200, body={"is_playing": False,
                                          "item": {"name": "x", "artists": []}}))
    no_binaries(monkeypatch)
    assert "already paused" in media.media("pause")


# -- status -----------------------------------------------------------------
def test_status_speaks_prose_with_no_markdown_or_urls(files, net):
    write_token()
    net.add("/me/player", Resp(200, body={
        "is_playing": True,
        "item": {"name": "Teardrop", "artists": [{"name": "Massive Attack"}],
                 "album": {"images": [{"url": "https://i.example/x.jpg"}]}},
        "device": {"name": "Cortana"}}))
    said = media.media("status")
    assert said == "Spotify is playing Teardrop by Massive Attack on Cortana."
    assert "http" not in said and "*" not in said


def test_status_with_nothing_playing_admits_it_cannot_see_other_players(files, net, monkeypatch):
    """Silence from a missing binary is IGNORANCE, not evidence. Saying
    "nothing else is playing" because playerctl is absent is a confident wrong
    answer, and it is only owned up to when the answer is otherwise bare."""
    write_token()
    net.add("/me/player", Resp(204))
    no_binaries(monkeypatch)
    said = media.media("status")
    assert "nothing loaded" in said and "playerctl" in said


def test_status_answers_for_BOTH_sources_not_the_first_one(files, net, monkeypatch):
    """A YouTube tab playing does not mean Spotify is silent. Reporting only
    whichever answered first was reporting half the room."""
    write_token()
    net.add("/me/player", Resp(200, body={
        "is_playing": False,
        "item": {"name": "Teardrop", "artists": [{"name": "Massive Attack"}]},
        "device": {"name": "Cortana"}}))
    no_binaries(monkeypatch, present=("playerctl",))
    fake_run(monkeypatch, out="Playing")
    said = media.media("status")
    assert "Spotify is paused on Teardrop" in said
    assert "playing here" in said


def test_status_names_a_paused_spotify_track_from_the_remembered_reading(files, net, monkeypatch):
    """Spotify answers 204 within a minute of pausing, so the API alone says
    "nothing". The dashboard already persists the last real reading - use it,
    because a paused track IS the answer to "what's playing"."""
    import json, time
    write_token()
    net.add("/me/player", Resp(204))
    no_binaries(monkeypatch)
    media.LAST_FILE.parent.mkdir(parents=True, exist_ok=True)
    media.LAST_FILE.write_text(json.dumps({
        "track": "Nightcall", "artist": "Kavinsky", "at": time.time() * 1000}))
    assert "paused on Nightcall by Kavinsky" in media.media("status")


def test_a_stale_remembered_track_is_not_reported_as_current(files, net, monkeypatch):
    import json, time
    write_token()
    net.add("/me/player", Resp(204))
    no_binaries(monkeypatch)
    media.LAST_FILE.parent.mkdir(parents=True, exist_ok=True)
    media.LAST_FILE.write_text(json.dumps({
        "track": "Yesterday", "artist": "x",
        "at": (time.time() - media.LAST_MAX_AGE - 60) * 1000}))
    said = media.media("status")
    assert "Yesterday" not in said and "nothing loaded" in said


def test_play_wakes_an_idle_desk_speaker_instead_of_doing_nothing(files, net, monkeypatch):
    """The reported bug: music would only start if the phone had already
    started it. PUT /me/player/play asks a device to resume ITS OWN context,
    and a spotifyd endpoint that has never played has none - so the press
    returned 204 and nothing happened. Waking it needs PUT /me/player."""
    write_token()
    no_binaries(monkeypatch)
    net.add("/me/player/devices",
            Resp(200, body={"devices": [{"id": "DESK", "name": "Cortana"}]}))
    net.add("/me/player", Resp(204))          # nothing active anywhere
    said = media.media("play")
    # The transfer is attempted (PUT /me/player with device_ids), and because
    # the account has no context to hand it, she says so instead of reporting
    # "Playing." into a silent room.
    assert any(m == "PUT" and u.endswith("/me/player")
               for m, u, _ in net.calls), net.calls
    assert "nothing queued" in said.lower(), said


def test_play_resumes_where_it_is_paused_rather_than_hijacking_the_device(files, net, monkeypatch):
    """Paused on the phone means resume ON THE PHONE. Dragging playback to the
    desk speaker because that is the device we know about would start the music
    in the wrong room."""
    write_token()
    no_binaries(monkeypatch)
    net.add("/me/player", Resp(200, body={
        "is_playing": False,
        "item": {"name": "Teardrop", "artists": [{"name": "Massive Attack"}]},
        "device": {"id": "PHONE", "name": "Pixel"}}))
    net.add("/me/player/play", Resp(204))
    assert media.media("play") == "Playing."
    # Resumed in place: it never went looking for the desk speaker, which is
    # what dragging playback to the wrong room would have looked like.
    assert not any("/me/player/devices" in u for u in net.paths()), net.paths()
    assert any(kw.get("params", {}).get("device_id") == "PHONE"
               for _, _, kw in net.calls), net.calls


def test_a_403_quotes_spotify_instead_of_blaming_premium(files, net, monkeypatch):
    """A 403 from the player endpoints is usually "Restriction violated", which
    has nothing to do with a subscription. Asserting Premium sent a real user
    off verifying an account that was fine, so the reason must come from
    Spotify, not from us."""
    write_token(scope=" ".join(media.PLAY_SCOPES))
    no_binaries(monkeypatch)
    net.add("/me/player/devices",
            Resp(200, body={"devices": [{"id": "DESK", "name": "Cortana"}]}))
    # Net matches by substring, FIRST hit wins - "/me/player" shadows
    # "/me/player/play", so the press route has to come first.
    net.add("/me/player/play", Resp(403, body={"error": {
        "status": 403, "message": "Player command failed: Restriction violated",
        "reason": "UNKNOWN"}}))
    net.add("/me/player", Resp(200, body={
        "is_playing": False,
        "item": {"name": "x", "artists": []},
        "device": {"id": "DESK", "name": "Cortana"}}))
    said = media.media("play")
    assert "Restriction violated" in said
    assert "Premium" not in said, said


def test_a_403_names_the_missing_scope_when_that_is_the_cause(files, net, monkeypatch):
    """The one 403 with an action attached. It is indistinguishable from the
    others by message alone, so the grant is checked first."""
    write_token(scope="user-read-playback-state")      # modify scope absent
    no_binaries(monkeypatch)
    net.add("/me/player/devices",
            Resp(200, body={"devices": [{"id": "DESK", "name": "Cortana"}]}))
    # Net matches by substring, FIRST hit wins - "/me/player" shadows
    # "/me/player/play", so the press route has to come first.
    net.add("/me/player/play", Resp(403, body={"error": {"message": "Forbidden"}}))
    net.add("/me/player", Resp(200, body={
        "is_playing": False,
        "item": {"name": "x", "artists": []},
        "device": {"id": "DESK", "name": "Cortana"}}))
    said = media.media("play")
    assert "user-modify-playback-state" in said
    assert "Reconnect Spotify" in said


def test_naming_a_track_wakes_an_idle_device_and_retries(files, net, monkeypatch):
    """The entry point that kept failing after bare play was fixed. Same cause -
    a registered-but-idle device refuses a play until something makes it active -
    but play_query has its own path, so the repair belongs in _press where every
    caller gets it."""
    write_token(scope=" ".join(media.PLAY_SCOPES))
    no_binaries(monkeypatch)
    net.add("/search", Resp(200, body={"tracks": {"items": [
        {"uri": "spotify:track:1", "name": "Nightcall",
         "artists": [{"name": "Kavinsky"}]}]}}))
    net.add("/me/player/devices",
            Resp(200, body={"devices": [{"id": "DESK", "name": "Cortana"}]}))

    # 403 until a transfer lands, 204 afterwards - exactly how an idle spotifyd
    # endpoint behaves.
    woken = {"yes": False}

    def transfer_ep(method, url, kw):
        woken["yes"] = True
        return Resp(204)

    def play_ep(method, url, kw):
        if not woken["yes"]:
            return Resp(403, body={"error": {
                "message": "Player command failed: Restriction violated",
                "reason": "UNKNOWN"}})
        return Resp(204)

    net.add("/me/player/play", play_ep)
    net.add("/me/player", transfer_ep)          # PUT /me/player = the transfer

    said = media.media("play_query", "Nightcall")
    assert woken["yes"], "never tried to wake the idle device"
    assert "Nightcall" in said and "Playing" in said, said


# -- play_query -------------------------------------------------------------
def test_play_query_plays_the_track_uri_it_found(files, net):
    write_token()
    net.add("/search", Resp(200, body={"tracks": {"items": [
        {"uri": "spotify:track:1", "name": "Nightcall",
         "artists": [{"name": "Kavinsky"}]}]}}))
    net.add("/me/player/devices",
            Resp(200, body={"devices": [{"id": "LOCAL", "name": "Cortana"}]}))
    net.add("/me/player/play", Resp(204))
    said = media.media("play_query", "nightcall")
    assert said == "Playing Nightcall by Kavinsky."
    play = [c for c in net.calls if c[1].endswith("/me/player/play")][0]
    assert play[2]["json"] == {"uris": ["spotify:track:1"]}
    assert play[2]["params"] == {"device_id": "LOCAL"}


def test_play_query_falls_back_to_an_artist_context(files, net):
    """No track matched "some radiohead", but the artist did. A context_uri
    shuffles their popular tracks, which is what the request actually meant."""
    write_token()
    net.add("/search", Resp(200, body={
        "tracks": {"items": []}, "albums": {"items": [None]},
        "artists": {"items": [{"uri": "spotify:artist:9", "name": "Radiohead"}]}}))
    net.add("/me/player/devices", Resp(200, body={"devices": []}))
    net.add("/me/player/play", Resp(204))
    assert media.media("play_query", "some radiohead") == "Playing Radiohead."
    play = [c for c in net.calls if c[1].endswith("/me/player/play")][0]
    assert play[2]["json"] == {"context_uri": "spotify:artist:9"}


def test_play_query_with_no_hits_says_so(files, net):
    write_token()
    net.add("/search", Resp(200, body={"tracks": {"items": []}}))
    assert "couldn't find" in media.media("play_query", "asdkjhasd")


def test_play_query_with_no_query_asks_rather_than_playing_something(files, net):
    write_token()
    said = media.media("play_query", "   ")
    assert said.endswith("?")
    assert net.calls == []


def test_a_bare_play_carrying_a_query_searches(files, net):
    """The model reaches for action=play with a query when it means play_query.
    Resuming whatever was on before is the wrong answer to "play Nightcall"."""
    write_token()
    net.add("/search", Resp(200, body={"tracks": {"items": [
        {"uri": "spotify:track:1", "name": "Nightcall", "artists": []}]}}))
    net.add("/me/player/devices", Resp(200, body={"devices": []}))
    net.add("/me/player/play", Resp(204))
    assert media.media("play", "nightcall") == "Playing Nightcall."
    assert any("/search" in u for u in net.paths())


# -- volume -----------------------------------------------------------------
def test_volume_never_touches_a_sink_input(files, net, monkeypatch):
    """audio_ducking.py owns sink-INPUT volumes: it captures the current value
    on engage and restores exactly that on release. A second writer there is
    silently reverted the next time Cortana finishes speaking, so this module
    stays on the default SINK, which covers spotifyd, the browser and VLC alike.
    """
    no_binaries(monkeypatch, present=("pactl",))
    calls = fake_run(monkeypatch)
    for args in [{"percent": 55}, {"query": "up"}, {"query": "down"},
                 {"query": "mute"}, {"query": "unmute"}]:
        media.media("volume", **args)
    assert calls, "no pactl call was made at all"
    assert not any("sink-input" in " ".join(c) for c in calls)
    assert all(media.SINK in c for c in calls)


def test_setting_a_level_unmutes_first(files, net, monkeypatch):
    """Setting a level on a muted sink changes nothing audible, which the user
    hears as the command being ignored."""
    no_binaries(monkeypatch, present=("pactl",))
    calls = fake_run(monkeypatch)
    media.media("volume", percent=30)
    assert calls[0] == ["pactl", "set-sink-mute", media.SINK, "0"]
    assert calls[1] == ["pactl", "set-sink-volume", media.SINK, "30%"]


def test_volume_is_clamped(files, net, monkeypatch):
    no_binaries(monkeypatch, present=("pactl",))
    calls = fake_run(monkeypatch)
    media.media("volume", percent=400)
    media.media("volume", percent=-9)
    levels = [c[3] for c in calls if c[1] == "set-sink-volume"]
    assert levels == ["100%", "0%"]


def test_missing_pactl_degrades_to_a_sentence(files, net, monkeypatch):
    no_binaries(monkeypatch)
    said = media.media("volume", percent=30)
    assert "pactl" in said


def test_volume_with_nothing_to_go_on_asks(files, net, monkeypatch):
    no_binaries(monkeypatch, present=("pactl",))
    fake_run(monkeypatch)
    assert "zero to a hundred" in media.media("volume")


# -- everything speaks ------------------------------------------------------
@pytest.mark.parametrize("action", list(media.ACTIONS) + ["nonsense"])
def test_every_action_returns_a_speakable_string(files, net, monkeypatch, action):
    """This tool's return value is read aloud verbatim. A dict, a None or a
    markdown bullet reaches the user as gibberish or as silence."""
    write_token()
    no_binaries(monkeypatch)
    said = media.media(action, "something", 50)
    assert isinstance(said, str) and said.strip()
    assert not said.startswith(("-", "*", "#"))
    assert "http" not in said and "\n" not in said


# -- pressing the device that is actually playing ---------------------------
def test_pause_presses_the_device_that_is_actually_playing(files, net):
    """Spotify is playing on the phone while spotifyd sits registered and idle.

    Aiming the pause at the local device then earns a 403 "restriction
    violated", which _http_sentence can only render as a Premium/scope problem.
    The user hears a confident, wrong explanation while the music keeps playing.
    _playback already named the active device, so use it - and spend no second
    request doing so.
    """
    write_token()
    net.add("/me/player/devices",
            Resp(200, body={"devices": [{"id": "LOCAL", "name": "Cortana"},
                                        {"id": "PHONE", "name": "Pixel"}]}))
    net.add("/me/player/pause", lambda m, u, kw: (
        Resp(204) if kw["params"].get("device_id") == "PHONE" else Resp(403)))
    net.add("/me/player", Resp(200, body={
        "is_playing": True, "item": {"name": "Song", "artists": []},
        "device": {"name": "Pixel", "id": "PHONE"}}))

    assert media.media("pause") == "Paused."
    presses = [c for c in net.calls if "/me/player/pause" in c[1]]
    assert len(presses) == 1, "pause cost a wasted request on the shared quota"
    assert presses[0][2]["params"] == {"device_id": "PHONE"}


def test_a_refused_targeted_press_falls_back_to_the_active_device(files, net):
    """next/previous deliberately skip the /me/player lookup to save a request,
    so they cannot know the phone is the thing playing. A targeted press then
    comes back 403 and the track never changes. One untargeted retry lets
    Spotify act on whatever is genuinely active."""
    write_token()
    net.add("/me/player/devices",
            Resp(200, body={"devices": [{"id": "LOCAL", "name": "Cortana"}]}))
    net.add("/me/player/next",
            lambda m, u, kw: Resp(403) if kw["params"] else Resp(204))
    assert media.media("next") == "Skipped forward."
    presses = [c[2]["params"] for c in net.calls if "/me/player/next" in c[1]]
    assert presses == [{"device_id": "LOCAL"}, {}]


def test_an_untargeted_press_is_never_retried(files, net, monkeypatch):
    """The retry exists only to undo OUR device targeting. Retrying a press that
    was already untargeted would double every failure against a quota three
    processes share, for no new information."""
    write_token()
    net.add("/me/player/devices", Resp(200, body={"devices": []}))
    net.add("/me/player/next", Resp(403))
    no_binaries(monkeypatch)
    media.media("next")
    assert len([c for c in net.calls if "/me/player/next" in c[1]]) == 1


# -- bodies and errors that are not shaped like the docs ---------------------
def test_a_search_body_that_is_not_json_is_a_sentence_not_a_traceback(files, net):
    """A 200 carrying HTML - captive portal, proxy error page - makes .json()
    raise. Unguarded, that reached the user as "TOOL ERROR (media): Expecting
    value: line 1 column 1". It must also NOT be reported as "nothing found":
    a real miss still carries {"tracks": {"items": []}}, so telling the user to
    try different words sends them chasing a search that never ran.
    """
    write_token()
    net.add("/search", Resp(200))          # body=None -> .json() raises
    said = media.media("play_query", "cello")
    assert isinstance(said, str)
    assert "couldn't find" not in said
    assert "didn't make sense" in said


def test_a_transport_error_does_not_read_urllib3_out_loud(files, net, monkeypatch,
                                                          capsys):
    """str() on a requests transport error is a urllib3 dump - host, port,
    errno, an object repr with a memory address. This return value is spoken
    verbatim, and the house rule is prose with no URLs. The detail belongs in
    the journal, which is the only place it can actually be read.
    """
    dump = ("HTTPSConnectionPool(host='api.spotify.com', port=443): Max retries "
            "exceeded with url: /v1/me/player/next (Caused by "
            "NewConnectionError('<urllib3.connection.HTTPSConnection object at "
            "0x7f2a>: [Errno 111] Connection refused'))")

    def boom(method, url, **kw):
        raise OSError(dump)
    monkeypatch.setattr(media, "_http", boom)
    write_token()
    no_binaries(monkeypatch)
    said = media.media("next")

    assert "couldn't reach Spotify" in said
    for noise in ("HTTPSConnectionPool", "urllib3", "Errno", "0x", "api.spotify.com"):
        assert noise not in said, f"{noise} would be read aloud"
    assert "HTTPSConnectionPool" in capsys.readouterr().out, "detail lost entirely"


def test_an_unexpected_failure_is_still_a_spoken_sentence(files, net, monkeypatch):
    """media() promises a string. Anything that escapes the per-action handlers
    - a payload shaped differently from the docs, a bad argument from the model
    - otherwise surfaces as "TOOL ERROR (media): ..." read at the user."""
    def boom():
        raise RuntimeError("something nobody predicted")
    monkeypatch.setattr(media, "_do_status", boom)
    said = media.media("status")
    assert isinstance(said, str) and said.strip()
    assert "something nobody predicted" not in said
    assert said.endswith(".")
