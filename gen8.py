#!/usr/bin/env python3
"""
Historical NHL -> ZenGM Hockey league-file generator (EARLY VERSION)
"""
from __future__ import annotations
import argparse
import gzip
import json
import math
import re
import statistics
import sys
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional
import requests

STATS_BASE = "https://api.nhle.com/stats/rest/en"
WEB_BASE = "https://api-web.nhle.com/v1"
MIN_NHL_YEAR = 1918
DEFAULT_DELAY = 0.15
USER_AGENT = "ZenGM-NHL-Historical-Generator/1.0 (Contact: admin@example.com)"

RATING_KEYS = (
    "hgt", "stre", "spd", "endu", "pss", "wst", "sst", "stk",
    "oiq", "chk", "blk", "fcf", "diq", "glk",
)

def clamp(x: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, x))

def round_int(x: float) -> int:
    return int(round(clamp(x)))

def parse_num(value: Any, default: float = 0.0) -> float:
    if value is None: return default
    s = str(value).strip().replace(",", "")
    if not s or s in {"-", "—", "–", "N/A", "NA", "null"}: return default
    s = s.replace("%", "")
    try: return float(s)
    except ValueError: return default

def parse_toi(toi_str: Any) -> float:
    if not toi_str or not isinstance(toi_str, str): return 0.0
    parts = toi_str.split(":")
    if len(parts) == 2:
        try: return float(parts[0]) + float(parts[1]) / 60.0
        except ValueError: return 0.0
    return 0.0

def clean_name(name: str) -> str:
    return re.sub(r"\s+", " ", name.replace("*", "")).strip()

def slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")

def split_name(name: str) -> tuple[str, str]:
    parts = name.split()
    if len(parts) == 1: return parts[0], ""
    return " ".join(parts[:-1]), parts[-1]

def safe_json_load(path: Path) -> dict[str, Any]:
    if path.suffix == ".gz":
        with gzip.open(path, "rt", encoding="utf-8") as f: return json.load(f)
    with path.open("r", encoding="utf-8") as f: return json.load(f)

def percentile(values: list[float], value: float) -> float:
    if not values: return 50.0
    vals = sorted(values)
    lo, hi = 0, len(vals)
    while lo < hi:
        mid = (lo + hi) // 2
        if vals[mid] < value: lo = mid + 1
        else: hi = mid
    first = lo
    while hi < len(vals) and vals[hi] == value: hi += 1
    return 100.0 * ((first + hi - 1) / 2 + 0.5) / len(vals)

def weighted(values: list[tuple[Optional[float], float]], fallback: float = 50.0) -> float:
    valid = [(v, w) for v, w in values if v is not None and math.isfinite(v)]
    if not valid: return fallback
    total = sum(w for _, w in valid)
    if total <= 0: return fallback
    return sum(v * w for v, w in valid) / total

class WebClient:
    def __init__(self, delay: float = DEFAULT_DELAY):
        self.delay = max(0.0, delay)
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": USER_AGENT,
            "Accept-Language": "en-US,en;q=0.9",
            "Accept": "application/json",
        })
        self._last_request = 0.0
        self._cache = {}

    def get(self, url: str, cache_name: str, force: bool = False) -> str:
        if cache_name in self._cache and not force:
            return self._cache[cache_name]
        
        for attempt in range(6):
            elapsed = time.monotonic() - self._last_request
            if elapsed < self.delay:
                time.sleep(self.delay - elapsed)
            try:
                r = self.session.get(url, timeout=45)
                self._last_request = time.monotonic()
                if r.status_code == 200:
                    if "application/json" in r.headers.get("Content-Type", "") or r.text.strip().startswith("{"):
                        self._cache[cache_name] = r.text
                        return r.text
                    else:
                        raise ValueError("Received non-JSON response")
                if r.status_code in {403, 429, 500, 502, 503, 504}:
                    time.sleep(min(60, 3 * (attempt + 1)))
                    continue
                r.raise_for_status()
            except requests.RequestException as exc:
                if attempt == 5:
                    raise RuntimeError(f"Could not fetch {url}: {exc}") from exc
                time.sleep(min(60, 3 * (attempt + 1)))
        raise RuntimeError(f"Could not fetch {url}")

def wikipedia_bio(name: str, client: WebClient) -> dict[str, Any]:
    import urllib.parse
    q = urllib.parse.quote(name)
    url = f"https://en.wikipedia.org/w/api.php?action=query&prop=extracts|pageimages&exintro=1&explaintext=1&piprop=original&format=json&titles={q}"
    try:
        text = client.get(url, "wiki_bio_" + slug(name) + ".json")
        data = json.loads(text)
        pages = data.get("query", {}).get("pages", {})
        page = next(iter(pages.values()), {})
        return {
            "extract": page.get("extract", ""),
            "image": (page.get("original") or {}).get("source", ""),
        }
    except Exception:
        return {}

@dataclass
class SeasonPlayer:
    name: str
    team: str
    pos: str
    age: float = 0.0
    href: str = ""
    stats: dict[str, float] = field(default_factory=dict)

@dataclass
class GoalieSeason:
    name: str
    team: str
    age: float = 0.0
    href: str = ""
    stats: dict[str, float] = field(default_factory=dict)

@dataclass
class Bio:
    first_name: str
    last_name: str
    height_inches: Optional[float] = None
    weight_lb: Optional[int] = None
    birth_year: Optional[int] = None
    birth_loc: str = ""
    position: str = ""
    shoots: str = ""
    draft_year: Optional[int] = None
    draft_round: Optional[int] = None
    draft_pick: Optional[int] = None
    draft_team: str = ""
    college: str = ""
    image_url: str = ""

class NHLAPI:
    def __init__(self, client: WebClient, skip_bios: bool = False, skip_wikipedia: bool = False):
        self.client = client
        self.skip_bios = skip_bios
        self.skip_wikipedia = skip_wikipedia

    def load_skater_rows(self, year: int) -> list[SeasonPlayer]:
        season_id = f"{year-1}{year}"
        url = f"{STATS_BASE}/skater/summary?limit=-1&start=0&cayenneExp=seasonId={season_id}"
        cache_name = f"skaters_{season_id}.json"
        text = self.client.get(url, cache_name)
        data = json.loads(text)
        players: list[SeasonPlayer] = []
        for row in data.get("data", []):
            name = clean_name(row.get("skaterFullName", ""))
            team_abbrevs = row.get("teamAbbrevs", "UNK")
            teams = [t.strip() for t in str(team_abbrevs).split(",")]
            team = teams[-1] if teams else "UNK"
            if not name or name.lower() in {"league average", "player"}: continue
            
            pos = str(row.get("positionCode", "F")).strip()
            toi_per_game = parse_toi(row.get("timeOnIcePerGame"))
            gp = parse_num(row.get("gamesPlayed"))
            spct_val = parse_num(row.get("shootingPct"))
            spct = spct_val * 100 if 0 < spct_val < 1 else spct_val
            fop_val = parse_num(row.get("faceoffWinPct"))
            fo_pct = fop_val * 100 if 0 < fop_val < 1 else fop_val
            
            stats = {
                "gp": gp, "g": parse_num(row.get("goals")), "a": parse_num(row.get("assists")),
                "pts": parse_num(row.get("points")), "plus_minus": parse_num(row.get("plusMinus")),
                "pim": parse_num(row.get("penaltyMinutes")), "evg": parse_num(row.get("evGoals")),
                "ppg": parse_num(row.get("ppGoals")), "shg": parse_num(row.get("shGoals")),
                "gwg": parse_num(row.get("gameWinningGoals")), "sog": parse_num(row.get("shots")),
                "spct": spct, "fo_pct": fo_pct, "atoi": toi_per_game, "toi": toi_per_game * gp,
                "blk": 0.0, "hit": 0.0, "take": 0.0, "give": 0.0, "fow": 0.0, "fol": 0.0,
            }
            players.append(SeasonPlayer(name=name, team=team, pos=pos, age=0.0, href=str(row.get("playerId", "")), stats=stats))
        return self._combine_skater_rows(players)

    @staticmethod
    def _combine_skater_rows(rows: list[SeasonPlayer]) -> list[SeasonPlayer]:
        grouped: dict[str, list[SeasonPlayer]] = defaultdict(list)
        for p in rows: grouped[p.name.lower()].append(p)
        out: list[SeasonPlayer] = []
        for _, parts in grouped.items():
            actual = [p for p in parts if p.team.upper() not in {"TOT", "TOTAL"}]
            if not actual: actual = parts
            chosen = actual[-1]
            combined = dict(chosen.stats)
            additive = {"gp", "g", "a", "pts", "plus_minus", "pim", "evg", "ppg", "shg", "gwg", "sog", "toi", "blk", "hit", "take", "give", "fow", "fol"}
            for k in additive: combined[k] = sum(p.stats.get(k, 0.0) for p in actual)
            gp = combined.get("gp", 0.0)
            if combined.get("sog", 0) > 0: combined["spct"] = 100 * combined.get("g", 0) / combined["sog"]
            fow_total = sum(p.stats.get("fow", 0.0) for p in actual)
            fol_total = sum(p.stats.get("fol", 0.0) for p in actual)
            if fow_total + fol_total > 0: combined["fo_pct"] = 100 * fow_total / (fow_total + fol_total)
            combined["atoi"] = combined["toi"] / max(gp, 1)
            out.append(SeasonPlayer(name=chosen.name, team=chosen.team, pos=chosen.pos, age=chosen.age or next((p.age for p in actual if p.age), 0.0), href=next((p.href for p in actual if p.href), ""), stats=combined))
        return out

    def load_goalie_rows(self, year: int) -> list[GoalieSeason]:
        season_id = f"{year-1}{year}"
        url = f"{STATS_BASE}/goalie/summary?limit=-1&start=0&cayenneExp=seasonId={season_id}"
        cache_name = f"goalies_{season_id}.json"
        text = self.client.get(url, cache_name)
        data = json.loads(text)
        goalies: list[GoalieSeason] = []
        for row in data.get("data", []):
            name = clean_name(row.get("goalieFullName", ""))
            team_abbrevs = row.get("teamAbbrevs", "UNK")
            teams = [t.strip() for t in str(team_abbrevs).split(",")]
            team = teams[-1] if teams else "UNK"
            if not name or name.lower() in {"league average", "player"}: continue
            sv_pct = parse_num(row.get("savePct"))
            if 0 < sv_pct < 1: sv_pct *= 100
            total_seconds = parse_num(row.get("timeOnIce"))
            mins = total_seconds / 60.0 if total_seconds > 0 else 0.0
            stats = {
                "gp": parse_num(row.get("gamesPlayed")), "gs": parse_num(row.get("gamesStarted")),
                "w": parse_num(row.get("wins")), "l": parse_num(row.get("losses")),
                "t": parse_num(row.get("ties")), "otl": parse_num(row.get("otLosses")),
                "ga": parse_num(row.get("goalsAgainst")), "sa": parse_num(row.get("shotsAgainst")),
                "sv": parse_num(row.get("saves")), "sv_pct": sv_pct,
                "gaa": parse_num(row.get("goalsAgainstAverage")), "so": parse_num(row.get("shutouts")),
                "min": mins, "gsaa": 0.0,
            }
            goalies.append(GoalieSeason(name=name, team=team, age=0.0, href=str(row.get("playerId", "")), stats=stats))
        return self._combine_goalie_rows(goalies)

    @staticmethod
    def _combine_goalie_rows(rows: list[GoalieSeason]) -> list[GoalieSeason]:
        grouped: dict[str, list[GoalieSeason]] = defaultdict(list)
        for p in rows: grouped[p.name.lower()].append(p)
        out: list[GoalieSeason] = []
        for _, parts in grouped.items():
            actual = [p for p in parts if p.team.upper() not in {"TOT", "TOTAL"}] or parts
            chosen = actual[-1]
            gp = sum(p.stats.get("gp", 0) for p in actual)
            gs = sum(p.stats.get("gs", 0) for p in actual)
            ga = sum(p.stats.get("ga", 0) for p in actual)
            sa = sum(p.stats.get("sa", 0) for p in actual)
            sv = sum(p.stats.get("sv", 0) for p in actual)
            mins = sum(p.stats.get("min", 0) for p in actual)
            wins = sum(p.stats.get("w", 0) for p in actual)
            losses = sum(p.stats.get("l", 0) for p in actual)
            ties = sum(p.stats.get("t", 0) for p in actual)
            otl = sum(p.stats.get("otl", 0) for p in actual)
            so = sum(p.stats.get("so", 0) for p in actual)
            stats = dict(chosen.stats)
            stats.update({"gp": gp, "gs": gs, "ga": ga, "sa": sa, "sv": sv, "min": mins, "w": wins, "l": losses, "t": ties, "otl": otl, "so": so})
            if sa > 0: stats["sv_pct"] = 100 * sv / sa
            if mins > 0: stats["gaa"] = ga * 60 / mins
            out.append(GoalieSeason(name=chosen.name, team=chosen.team, age=chosen.age or next((p.age for p in actual if p.age), 0.0), href=next((p.href for p in actual if p.href), ""), stats=stats))
        return out

    def load_bio(self, href: str, fallback_name: str) -> Bio:
        first, last = split_name(fallback_name)
        
        if self.skip_bios:
            return Bio(first, last)
        
        if not href or not str(href).isdigit():
            bio = Bio(first, last)
            if not self.skip_wikipedia:
                wiki = wikipedia_bio(fallback_name, self.client)
                if wiki.get("extract"):
                    m = re.search(r"(?:born|was born)[^0-9]*(\d{4})", wiki["extract"], re.I)
                    if m: bio.birth_year = int(m.group(1))
                    m = re.search(r"born[^,;]*,\s*(?:in\s+)?([^.;]+)", wiki["extract"], re.I)
                    if m: bio.birth_loc = m.group(1).strip()
                    if not bio.image_url: bio.image_url = wiki.get("image", "")
            return bio
        
        cache_name = f"player_bio_{href}.json"
        url = f"{WEB_BASE}/player/{href}/landing"
        try:
            text = self.client.get(url, cache_name)
            data = json.loads(text)
        except Exception:
            return Bio(first, last)
        
        first = data.get("firstName", {}).get("default", "")
        last = data.get("lastName", {}).get("default", "")
        bio = Bio(first, last)
        bio.height_inches = data.get("heightInInches")
        bio.weight_lb = data.get("weightInPounds")
        birth_date = data.get("birthDate")
        if birth_date: bio.birth_year = int(str(birth_date).split("-")[0])
        city = data.get("birthCity", {}).get("default", "")
        country = data.get("birthCountry", "")
        bio.birth_loc = f"{city}, {country}".strip(", ") if city or country else ""
        bio.shoots = data.get("shootsCatches", "")
        bio.position = str(data.get("positionCode") or data.get("position") or "").strip().upper()
        bio.image_url = data.get("headshot", "")
        
        if not self.skip_wikipedia:
            wiki = wikipedia_bio(fallback_name, self.client)
            if wiki.get("extract"):
                extract = wiki["extract"]
                if not bio.draft_year:
                    m = re.search(r"(\d{4})\s+.*\s+draft", extract, re.I)
                    if m: bio.draft_year = int(m.group(1))
                if not bio.birth_loc and not bio.birth_year:
                    m = re.search(r"(?:born|was born)[^0-9]*(\d{4})", extract, re.I)
                    if m: bio.birth_year = int(m.group(1))
                    m = re.search(r"born[^,;]*,\s*(?:in\s+)?([^.;]+)", extract, re.I)
                    if m: bio.birth_loc = m.group(1).strip()
                if not bio.image_url: bio.image_url = wiki.get("image", "")
        return bio

    def load_teams(self, year: int, skaters: list[SeasonPlayer], goalies: list[GoalieSeason]) -> list[dict[str, Any]]:
        teams_map: dict[str, dict[str, Any]] = {}
        for p in skaters + goalies:
            code = p.team.upper()
            if code not in teams_map and len(code) <= 5:
                region, name = canonical_team_name(code, code)
                conference, division = historical_alignment(year, code) or ("", "")
                teams_map[code] = {"hr_code": code, "name": f"{region} {name}", "conference": conference, "division": division, "games": 80 if year == 1987 else 82}
        return list(teams_map.values())

TEAM_ALIASES = {
    "ANA": ("Anaheim", "Ducks"), "MDA": ("Anaheim", "Mighty Ducks"), "ARI": ("Arizona", "Coyotes"),
    "ATF": ("Atlanta", "Flames"), "ATL": ("Atlanta", "Thrashers"), "BOS": ("Boston", "Bruins"),
    "BUF": ("Buffalo", "Sabres"), "CAL": ("California", "Golden Seals"), "CAR": ("Carolina", "Hurricanes"),
    "CBH": ("Chicago", "Black Hawks"), "CBJ": ("Columbus", "Blue Jackets"), "CHI": ("Chicago", "Blackhawks"),
    "CGS": ("California", "Golden Seals"), "CGY": ("Calgary", "Flames"), "CLE": ("Cleveland", "Barons"),
    "COL": ("Colorado", "Avalanche"), "DAL": ("Dallas", "Stars"), "DET": ("Detroit", "Red Wings"),
    "EDM": ("Edmonton", "Oilers"), "FLA": ("Florida", "Panthers"), "HAM": ("Hamilton", "Tigers"),
    "HAR": ("Hartford", "Whalers"), "KCS": ("Kansas City", "Scouts"), "LAK": ("Los Angeles", "Kings"),
    "MIN": ("Minnesota", "Wild"), "MNS": ("Minnesota", "North Stars"), "MTL": ("Montreal", "Canadiens"),
    "MTM": ("Montreal", "Maroons"), "MWN": ("Montreal", "Wanderers"), "NSH": ("Nashville", "Predators"),
    "NJD": ("New Jersey", "Devils"), "NYA": ("New York", "Americans"), "NYI": ("New York", "Islanders"),
    "NYR": ("New York", "Rangers"), "OAK": ("Oakland", "Seals"), "OTT": ("Ottawa", "Senators"),
    "OTS": ("Ottawa", "Senators"), "PHI": ("Philadelphia", "Flyers"), "PHQ": ("Philadelphia", "Quakers"),
    "PIT": ("Pittsburgh", "Penguins"), "PTP": ("Pittsburgh", "Pirates"), "QBD": ("Quebec", "Bulldogs"),
    "QUE": ("Quebec", "Nordiques"), "SEA": ("Seattle", "Kraken"), "SJS": ("San Jose", "Sharks"),
    "SLE": ("St. Louis", "Eagles"), "STL": ("St. Louis", "Blues"), "TAN": ("Toronto", "Arenas"),
    "TBL": ("Tampa Bay", "Lightning"), "TOR": ("Toronto", "Maple Leafs"), "TRA": ("Toronto", "Arenas"),
    "TRI": ("Toronto", "St. Patricks"), "TRS": ("Toronto", "St. Patricks"), "UTA": ("Utah", "Mammoth"),
    "VAN": ("Vancouver", "Canucks"), "VGK": ("Vegas", "Golden Knights"), "WHA": ("Winnipeg", "Jets"),
    "WPG": ("Winnipeg", "Jets"), "WSH": ("Washington", "Capitals"), "WIN": ("Winnipeg", "Jets"),
    "HFD": ("Hartford", "Whalers"),
}

def canonical_team_name(code: str, fallback: str = "") -> tuple[str, str]:
    if code in TEAM_ALIASES: return TEAM_ALIASES[code]
    if fallback:
        parts = fallback.split()
        if len(parts) > 1: return " ".join(parts[:-1]), parts[-1]
        return fallback, ""
    return code, code

HISTORICAL_ALIGNMENTS = {
    1987: {
        "Adams Division": {"BOS", "BUF", "HFD", "MTL", "QUE"},
        "Patrick Division": {"NJD", "NYI", "NYR", "PHI", "PIT", "WSH"},
        "Norris Division": {"CHI", "DET", "MNS", "STL", "TOR"},
        "Smythe Division": {"CGY", "EDM", "LAK", "VAN", "WIN"},
    },
}

def historical_alignment(year: int, code: str) -> tuple[str, str] | None:
    table = HISTORICAL_ALIGNMENTS.get(year)
    if not table: return None
    for division, teams in table.items():
        if code in teams:
            conference = "Prince of Wales Conference" if division in {"Adams Division", "Patrick Division"} else "Clarence Campbell Conference"
            return conference, division
    return None

def normalize_team_abbrev(code: str) -> str:
    aliases = {
        "CBH": "CHI", "MTM": "MTL", "TRS": "TOR", "TRA": "TOR", "TRI": "TOR", "TAN": "TOR",
        "OTS": "OTT", "PTP": "PIT", "ATF": "CGY", "KCS": "NJD", "MNS": "DAL", "QUE": "COL",
        "QBD": "MTL", "HAM": "MTL", "MWN": "MTL", "HAR": "CAR", "WHA": "UTA", "NYA": "NYI",
        "OAK": "SJS", "CGS": "SJS", "CAL": "SJS", "CLE": "NJD", "MDA": "ANA", "ARI": "UTA",
        "ATL": "WPG", "PHQ": "PIT", "SLE": "STL",
    }
    return aliases.get(code, code)

def get_final_position(player: SeasonPlayer, bio: Bio) -> str:
    """
    Position detection priority:
    1. Stats API position code (most reliable)
    2. Bio position
    3. Faceoff percentage >40% (center)
    4. Default to wing based on handedness
    """
    # PRIORITY 1: Stats API position code
    p = player.pos.strip().upper()
    if p in {"D", "LD", "RD", "DEF"}: return "D"
    if p in {"C", "CE"}: return "C"
    if p in {"L", "LW", "LF"}: return "L"
    if p in {"R", "RW", "RF"}: return "R"
    
    # PRIORITY 2: Bio position
    if bio and bio.position:
        bp = bio.position.strip().upper()
        if bp in {"D", "LD", "RD", "DEF"}: return "D"
        if bp in {"C", "CE"}: return "C"
        if bp in {"L", "LW", "LF"}: return "L"
        if bp in {"R", "RW", "RF"}: return "R"
    
    # PRIORITY 3: Faceoff percentage (only true centers take >40% faceoffs)
    stats = player.stats
    if stats.get("fo_pct", 0) > 40: return "C"
    
    # PRIORITY 4: Default to wing based on handedness
    if bio and bio.shoots:
        shoots = bio.shoots.upper()
        if shoots == "L": return "L"
        if shoots == "R": return "R"
    
    return "L"  # Default to left wing

class RatingEngine:
    def __init__(self, skaters: dict[int, list[SeasonPlayer]], goalies: dict[int, list[GoalieSeason]]):
        self.skaters = skaters
        self.goalies = goalies
        self.skater_dist: dict[int, dict[str, list[float]]] = {}
        self.goalie_dist: dict[int, dict[str, list[float]]] = {}
        self.d_dist: dict[int, dict[str, list[float]]] = {}
        self.f_dist: dict[int, dict[str, list[float]]] = {}
        
        for year, players in skaters.items():
            self.skater_dist[year] = self._skater_distributions(players)
            defensemen = [p for p in players if p.pos.upper() in {"D", "LD", "RD", "DEF"}]
            forwards = [p for p in players if p.pos.upper() not in {"D", "LD", "RD", "DEF", "G"}]
            self.d_dist[year] = self._skater_distributions(defensemen)
            self.f_dist[year] = self._skater_distributions(forwards)
        
        for year, players in goalies.items():
            self.goalie_dist[year] = self._goalie_distributions(players)

    @staticmethod
    def _skater_distributions(players: list[SeasonPlayer]) -> dict[str, list[float]]:
        d: dict[str, list[float]] = defaultdict(list)
        for p in players:
            gp = max(p.stats.get("gp", 0), 1)
            if gp < 5: continue
            rates = {
                "g60": p.stats.get("g", 0) / gp * 60, "a60": p.stats.get("a", 0) / gp * 60,
                "p60": p.stats.get("pts", 0) / gp * 60, "s60": p.stats.get("sog", 0) / gp * 60,
                "pm60": p.stats.get("plus_minus", 0) / gp * 60, "pim60": p.stats.get("pim", 0) / gp * 60,
                "hit60": p.stats.get("hit", 0) / gp * 60, "blk60": p.stats.get("blk", 0) / gp * 60,
                "take60": p.stats.get("take", 0) / gp * 60, "give60": p.stats.get("give", 0) / gp * 60,
                "toi": p.stats.get("atoi", 0), "gp": gp,
            }
            for k, v in rates.items():
                if math.isfinite(v): d[k].append(v)
            spct = p.stats.get("spct", 0)
            if spct > 0: d["spct"].append(spct)
            fop = p.stats.get("fo_pct", 0)
            if fop > 0: d["fo_pct"].append(fop)
        return d

    @staticmethod
    def _goalie_distributions(players: list[GoalieSeason]) -> dict[str, list[float]]:
        d: dict[str, list[float]] = defaultdict(list)
        for p in players:
            gp = p.stats.get("gp", 0)
            if gp < 5: continue
            for k in ("sv_pct", "gaa", "gsaa", "w"):
                v = p.stats.get(k, 0)
                if v: d[k].append(v)
            d["gp"].append(gp)
        return d

    def _pct(self, year: int, key: str, value: float, pos_type: str = "all") -> float:
        if pos_type == "D":
            vals = self.d_dist.get(year, {}).get(key, [])
        elif pos_type == "F":
            vals = self.f_dist.get(year, {}).get(key, [])
        else:
            vals = self.skater_dist.get(year, {}).get(key, [])
        if not vals: return 50.0
        return percentile(vals, value)

    def defenseman_ratings(self, player: SeasonPlayer, year: int, bio: Bio) -> dict[str, float]:
        """DEFENSEMEN: LOW offense, HIGH defense"""
        s = player.stats
        gp = max(s.get("gp", 0), 1)
        
        pm60 = s.get("plus_minus", 0) / gp * 60
        hit60 = s.get("hit", 0) / gp * 60
        blk60 = s.get("blk", 0) / gp * 60
        take60 = s.get("take", 0) / gp * 60
        give60 = s.get("give", 0) / gp * 60
        toi = s.get("atoi", 0)
        p60 = s.get("pts", 0) / gp * 60
        a60 = s.get("a", 0) / gp * 60
        
        pmp = self._pct(year, "pm60", pm60, "D")
        hitp = self._pct(year, "hit60", hit60, "D")
        blkp = self._pct(year, "blk60", blk60, "D")
        takep = self._pct(year, "take60", take60, "D")
        givep = self._pct(year, "give60", give60, "D")
        toip = self._pct(year, "toi", toi, "D")
        pp = self._pct(year, "p60", p60, "D")
        apct = self._pct(year, "a60", a60, "D")
        
        defense_score = 0.30 * pmp + 0.25 * blkp + 0.20 * hitp + 0.15 * takep + 0.10 * (100 - givep)
        quality_pct = clamp(0.65 * defense_score + 0.20 * toip + 0.15 * (0.6 * pp + 0.4 * apct))
        
        q = 38.0 + 25.0 * (quality_pct / 100.0) ** 2.0
        if quality_pct >= 98: q += (quality_pct - 98) * 4.0
        q = clamp(q, 38, 78)
        
        hgt = clamp(35 + (bio.height_inches - 66) * 5.2) if bio.height_inches else q + 5
        stre_body = clamp(42 + (bio.weight_lb - 185) * 0.25) if bio.weight_lb else q + 3
        
        speed = clamp(q - 2 + 0.08 * (toip - 50), 35, 85)
        endurance = clamp(q + 0.12 * (toip - 50), 35, 88)
        
        # OFFENSIVE RATINGS: LOW for defensemen
        passing = clamp(q - 14 + 0.10 * (apct - 50), 30, 75)
        wrist = clamp(q - 18 + 0.08 * (pp - 50), 28, 70)
        slap = clamp(q - 15 + 0.10 * (pp - 50), 30, 72)
        stick = clamp(q - 12 + 0.08 * (apct - 50), 32, 78)
        offense_iq = clamp(q - 14 + 0.10 * (pp - 50), 30, 75)
        
        # DEFENSIVE RATINGS: HIGH for defensemen
        checking = clamp(q + 8 + 0.15 * (hitp - 50) + 0.10 * (stre_body - 50), 42, 90)
        blocking = clamp(q + 10 + 0.18 * (blkp - 50) + 0.08 * (hgt - 60), 44, 92)
        defense_iq = clamp(q + 8 + 0.18 * (pmp - 50) + 0.10 * (toip - 50), 44, 90)
        
        faceoffs = 25
        
        ratings = {
            "hgt": round_int(hgt), "stre": round_int(stre_body * 0.50 + q * 0.50),
            "spd": round_int(speed), "endu": round_int(endurance), "pss": round_int(passing),
            "wst": round_int(wrist), "sst": round_int(slap), "stk": round_int(stick),
            "oiq": round_int(offense_iq), "chk": round_int(checking), "blk": round_int(blocking),
            "fcf": round_int(faceoffs), "diq": round_int(defense_iq), "glk": 0,
        }
        
        for k in RATING_KEYS:
            if k != "glk": ratings[k] = int(clamp(ratings[k], 20, 92))
        
        return ratings

    def forward_ratings(self, player: SeasonPlayer, year: int, bio: Bio, pos: str) -> dict[str, float]:
        """FORWARDS: HIGH offense, LOW defense"""
        s = player.stats
        gp = max(s.get("gp", 0), 1)
        
        p60 = s.get("pts", 0) / gp * 60
        g60 = s.get("g", 0) / gp * 60
        a60 = s.get("a", 0) / gp * 60
        s60 = s.get("sog", 0) / gp * 60
        spct = s.get("spct", 0)
        toi = s.get("atoi", 0)
        pm60 = s.get("plus_minus", 0) / gp * 60
        hit60 = s.get("hit", 0) / gp * 60
        blk60 = s.get("blk", 0) / gp * 60
        take60 = s.get("take", 0) / gp * 60
        give60 = s.get("give", 0) / gp * 60
        fop = s.get("fo_pct", 0)
        
        pp = self._pct(year, "p60", p60, "F")
        gpct = self._pct(year, "g60", g60, "F")
        apct = self._pct(year, "a60", a60, "F")
        spctile = self._pct(year, "s60", s60, "F")
        shotp = self._pct(year, "spct", spct, "F") if spct else 50
        toip = self._pct(year, "toi", toi, "F")
        pmp = self._pct(year, "pm60", pm60, "F")
        hitp = self._pct(year, "hit60", hit60, "F")
        blkp = self._pct(year, "blk60", blk60, "F")
        takep = self._pct(year, "take60", take60, "F")
        givep = self._pct(year, "give60", give60, "F")
        fopct = self._pct(year, "fo_pct", fop, "F") if fop else 50
        
        is_center = (pos == "C")
        
        offense_score = 0.35 * pp + 0.25 * gpct + 0.20 * apct + 0.10 * spctile + 0.10 * shotp
        defense_score = 0.30 * pmp + 0.20 * takep + 0.15 * blkp + 0.15 * (100 - givep) + 0.20 * toip
        quality_pct = clamp(0.60 * offense_score + 0.20 * defense_score + 0.20 * toip)
        if is_center and fop: quality_pct = clamp(quality_pct + 0.05 * (fopct - 50))
        
        q = 40.0 + 28.0 * (quality_pct / 100.0) ** 2.2
        if quality_pct >= 99.5: q += (quality_pct - 99.5) * 6.0
        q = clamp(q, 40, 82)
        
        hgt = clamp(35 + (bio.height_inches - 68) * 4.8) if bio.height_inches else q - 3
        stre_body = clamp(42 + (bio.weight_lb - 175) * 0.20) if bio.weight_lb else q - 2
        
        speed = clamp(q + 0.10 * (toip - 50), 35, 88)
        endurance = clamp(q + 0.10 * (toip - 50), 35, 88)
        
        # OFFENSIVE RATINGS: HIGH for forwards
        passing = clamp(q + 8 + 0.15 * (apct - 50) + 0.08 * (pp - 50), 35, 92)
        wrist = clamp(q + 10 + 0.18 * (gpct - 50) + 0.10 * (shotp - 50), 35, 92)
        slap = clamp(q + 6 + 0.12 * (spctile - 50) + 0.10 * (gpct - 50), 35, 90)
        stick = clamp(q + 8 + 0.14 * (pp - 50) + 0.10 * (apct - 50), 35, 92)
        offense_iq = clamp(q + 10 + 0.18 * (pp - 50) + 0.10 * (apct - 50), 35, 92)
        
        # DEFENSIVE RATINGS: LOW for forwards
        checking = clamp(q - 8 + 0.10 * (hitp - 50) + 0.06 * (stre_body - 50), 28, 85)
        blocking = clamp(q - 12 + 0.08 * (blkp - 50), 25, 80)
        defense_iq = clamp(q - 6 + 0.12 * (pmp - 50) + 0.08 * (defense_score - 50), 30, 85)
        
        if is_center and fop:
            faceoffs = clamp(50 + 0.55 * (fopct - 50) + 0.10 * (q - 50))
        elif is_center:
            faceoffs = clamp(q - 3)
        else:
            faceoffs = 25
        
        ratings = {
            "hgt": round_int(hgt), "stre": round_int(stre_body * 0.45 + q * 0.55),
            "spd": round_int(speed), "endu": round_int(endurance), "pss": round_int(passing),
            "wst": round_int(wrist), "sst": round_int(slap), "stk": round_int(stick),
            "oiq": round_int(offense_iq), "chk": round_int(checking), "blk": round_int(blocking),
            "fcf": round_int(faceoffs), "diq": round_int(defense_iq), "glk": 0,
        }
        
        for k in RATING_KEYS:
            if k != "glk": ratings[k] = int(clamp(ratings[k], 20, 92))
        
        return ratings

    def goalie_rating(self, goalie: GoalieSeason, year: int, bio: Bio) -> dict[str, float]:
        """GOALIES: Higher base ratings"""
        s = goalie.stats
        sv = s.get("sv_pct", 0)
        gaa = s.get("gaa", 0)
        gp = s.get("gp", 0)
        wins = s.get("w", 0)
        dist = self.goalie_dist.get(year, {})
        
        if dist.get("sv_pct") and sv:
            svp = percentile(dist["sv_pct"], sv)
            gaap = percentile(dist.get("gaa", []), gaa) if dist.get("gaa") and gaa else 50
            gaa_quality = 100 - gaap
            workload = percentile(dist.get("gp", []), gp) if dist.get("gp") else 50
            glk = 0.65 * svp + 0.25 * gaa_quality + 0.10 * workload
        else:
            gaap = percentile(dist.get("gaa", []), gaa) if dist.get("gaa") and gaa else 50
            gaa_quality = 100 - gaap
            win_pct = percentile(dist.get("w", []), wins) if dist.get("w") and wins else 50
            workload = percentile(dist.get("gp", []), gp) if dist.get("gp") else 50
            glk = 0.70 * gaa_quality + 0.20 * win_pct + 0.10 * workload
            
        if gp < 5: glk = 45
        
        # HIGHER base rating for goalies
        glk_rating = 45 + 30 * (glk / 100) ** 1.8
        if glk >= 99: glk_rating += (glk - 99) * 6.0
        glk_rating = clamp(glk_rating, 40, 85)

        return {
            "hgt": round_int((bio.height_inches - 64) / 18 * 100) if bio.height_inches else 55,
            "stre": 50, "spd": 30, "endu": 50, "pss": 25, "wst": 25, "sst": 25, "stk": 25,
            "oiq": 25, "chk": 25, "blk": 50, "fcf": 25, "diq": 55, "glk": round_int(glk_rating),
        }

def template_game_attributes(template: Optional[dict[str, Any]], year: int, nteams: int, divs: list[dict[str, Any]], confs: list[dict[str, Any]], num_games: int) -> dict[str, Any]:
    if template: attrs = dict(template.get("gameAttributes", {}))
    else: attrs = {}
    transient = {"gameOver", "phase", "daysLeft", "userTid", "userTids", "spectator", "godMode", "godModeInPast", "expansionDraft", "tradeProposalsSeed"}
    attrs = {k: v for k, v in attrs.items() if k not in transient}
    attrs.update({"season": year, "startingSeason": year, "phase": 0, "daysLeft": 0, "gameOver": False, "godMode": False, "spectator": False})
    attrs["confs"] = confs if confs else [{"cid": 0, "name": "Eastern Conference"}, {"cid": 1, "name": "Western Conference"}]
    attrs["divs"] = divs if divs else [{"did": 0, "cid": 0, "name": "Division"}]
    attrs.update({"numGames": num_games, "numGamesDiv": None, "numGamesConf": None, "userTid": [{"start": None, "value": 0}], "userTids": [0]})
    return attrs

def assign_conference_division(hr_teams: list[dict[str, Any]]) -> tuple[dict[str, tuple[int, int]], list[dict[str, Any]], list[dict[str, Any]]]:
    conf_names: list[str] = []
    div_names: list[tuple[str, str]] = []
    for t in hr_teams:
        c = t.get("conference", "").strip()
        d = t.get("division", "").strip()
        if c and c not in conf_names: conf_names.append(c)
        if d and (d, c) not in div_names: div_names.append((d, c))
    if not conf_names: conf_names = ["Eastern Conference", "Western Conference"]
    if len(conf_names) > 2: conf_names = conf_names[:2]
    
    divs: list[dict[str, Any]] = []
    div_index: dict[tuple[str, str], int] = {}
    for d, c in div_names:
        cid = conf_names.index(c) if c in conf_names else (0 if len(divs) % 2 == 0 else 1)
        if (d, c) in div_index: continue
        if len(divs) >= 6: break
        div_index[(d, c)] = len(divs)
        divs.append({"did": len(divs), "cid": cid, "name": d})
    if not divs: divs = [{"did": 0, "cid": 0, "name": "Division"}, {"did": 1, "cid": 1, "name": "Division"}]
    
    align: dict[str, tuple[int, int]] = {}
    for i, t in enumerate(hr_teams):
        c = t.get("conference", "")
        d = t.get("division", "")
        if (d, c) in div_index:
            did = div_index[(d, c)]
            cid = divs[did]["cid"]
        else:
            cid = 0 if i < math.ceil(len(hr_teams) / 2) else 1
            candidates = [x for x in divs if x["cid"] == cid]
            did = candidates[0]["did"] if candidates else 0
        align[t["hr_code"]] = (cid, did)
    confs = [{"cid": cid, "name": (conf_names[cid] if cid < len(conf_names) else "Conference")} for cid in sorted({d["cid"] for d in divs})]
    return align, divs, confs

def make_teams(hr_teams: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, int], list[dict[str, Any]], list[dict[str, Any]]]:
    align, divs, confs = assign_conference_division(hr_teams)
    teams: list[dict[str, Any]] = []
    tid_by_hr: dict[str, int] = {}
    for tid, t in enumerate(hr_teams):
        code = t["hr_code"]
        region, name = canonical_team_name(code, t.get("name", ""))
        cid, did = align[code]
        teams.append({"tid": tid, "cid": cid, "did": did, "region": region, "name": name, "abbrev": code, "pop": 5.0, "stadiumCapacity": 17500, "disabled": False})
        tid_by_hr[code] = tid
    return teams, tid_by_hr, divs, confs

def combine_three_rating_sets(prev: Optional[dict[str, float]], target: Optional[dict[str, float]], future: Optional[dict[str, float]]) -> dict[str, int]:
    out: dict[str, int] = {}
    for key in RATING_KEYS:
        vals = [(prev.get(key) if prev else None, 0.70), (target.get(key) if target else None, 0.20), (future.get(key) if future else None, 0.10)]
        out[key] = round_int(weighted(vals, fallback=50))
    return out

def make_player(name: str, tid: int, bio: Bio, pos: str, ratings: dict[str, int], draft_team_lookup: dict[str, int]) -> dict[str, Any]:
    first, last = split_name(name)
    ratings_out = dict(ratings)
    ratings_out["pos"] = pos
    p: dict[str, Any] = {"firstName": first, "lastName": last, "tid": tid, "ratings": [ratings_out]}
    if bio.birth_year: p["born"] = {"year": bio.birth_year}
    if bio.birth_loc: p["born"]["loc"] = bio.birth_loc
    if bio.height_inches: p["hgt"] = round_int(bio.height_inches)
    if bio.weight_lb: p["weight"] = bio.weight_lb
    if bio.image_url: p["imgURL"] = bio.image_url
    if bio.draft_year:
        draft: dict[str, Any] = {"year": bio.draft_year}
        if bio.draft_round is not None: draft["round"] = bio.draft_round
        if bio.draft_pick is not None: draft["pick"] = bio.draft_pick
        if bio.draft_team:
            tid_match = draft_team_lookup.get(normalize_team_abbrev(bio.draft_team.upper()))
            if tid_match is not None: draft["tid"] = tid_match
        p["draft"] = draft
    if bio.college: p["college"] = bio.college
    return p

FRANCHISE_ALIASES = {
    "WHA": "WIN", "WIN": "WIN", "QUE": "COL", "COL": "COL", "MNS": "DAL", "DAL": "DAL",
    "HFD": "CAR", "HAR": "CAR", "CAR": "CAR", "MDA": "ANA", "ANA": "ANA", "ATF": "CGY",
    "CGY": "CGY", "ATL": "ATL", "ARI": "WIN",
}

def franchise_key(code: str) -> str: return FRANCHISE_ALIASES.get(code.upper(), code.upper())

def make_scheduled_events(start_year: int, existing_hr_codes: set[str], target_tid_by_hr: dict[str, int], divs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    franchise_tid: dict[str, int] = {}
    for code, tid in target_tid_by_hr.items():
        franchise_tid[code] = tid
        franchise_tid[franchise_key(code)] = tid
    next_tid = max(target_tid_by_hr.values(), default=-1) + 1
    
    MODERN_EVENTS = [
        (1967, "expansion", "OAK", "Oakland", "Seals"), (1967, "expansion", "LAK", "Los Angeles", "Kings"),
        (1967, "expansion", "MNS", "Minnesota", "North Stars"), (1967, "expansion", "PHI", "Philadelphia", "Flyers"),
        (1967, "expansion", "PIT", "Pittsburgh", "Penguins"), (1967, "expansion", "STL", "St. Louis", "Blues"),
        (1970, "expansion", "BUF", "Buffalo", "Sabres"), (1970, "expansion", "VAN", "Vancouver", "Canucks"),
        (1972, "expansion", "ATF", "Atlanta", "Flames"), (1972, "expansion", "NYI", "New York", "Islanders"),
        (1974, "expansion", "WSH", "Washington", "Capitals"), (1974, "expansion", "KCS", "Kansas City", "Scouts"),
        (1976, "relocation", "KCS", "Denver", "Colorado Rockies"), (1976, "relocation", "OAK", "Cleveland", "Barons"),
        (1978, "contraction", "CLE", "Cleveland", "Barons"), (1979, "expansion", "EDM", "Edmonton", "Oilers"),
        (1979, "expansion", "HAR", "Hartford", "Whalers"), (1979, "expansion", "QUE", "Quebec", "Nordiques"),
        (1979, "expansion", "WHA", "Winnipeg", "Jets"), (1980, "relocation", "ATF", "Calgary", "Flames"),
        (1982, "relocation", "COL", "New Jersey", "Devils"), (1991, "expansion", "SJS", "San Jose", "Sharks"),
        (1992, "expansion", "OTT", "Ottawa", "Senators"), (1992, "expansion", "TBL", "Tampa Bay", "Lightning"),
        (1993, "expansion", "MDA", "Anaheim", "Mighty Ducks"), (1993, "expansion", "FLA", "Florida", "Panthers"),
        (1993, "relocation", "MNS", "Dallas", "Stars"), (1995, "relocation", "QUE", "Denver", "Colorado Avalanche"),
        (1995, "relocation", "HAR", "Raleigh", "Carolina Hurricanes"), (1996, "relocation", "WHA", "Phoenix", "Coyotes"),
        (1998, "expansion", "NSH", "Nashville", "Predators"), (1999, "expansion", "ATL", "Atlanta", "Thrashers"),
        (2000, "expansion", "MIN", "Minnesota", "Wild"), (2000, "expansion", "CBJ", "Columbus", "Blue Jackets"),
        (2006, "teaminfo", "MDA", "Anaheim", "Ducks"), (2011, "relocation", "ATL", "Winnipeg", "Jets"),
        (2017, "expansion", "VGK", "Vegas", "Golden Knights"), (2021, "expansion", "SEA", "Seattle", "Kraken"),
        (2024, "relocation", "ARI", "Utah", "Mammoth"),
    ]
    EARLY_EVENTS = [
        (1919, "expansion", "QBD", "Quebec", "Bulldogs"), (1920, "relocation", "QBD", "Hamilton", "Tigers"),
        (1924, "expansion", "BOS", "Boston", "Bruins"), (1924, "expansion", "MTM", "Montreal", "Maroons"),
        (1925, "expansion", "NYA", "New York", "Americans"), (1925, "expansion", "PTP", "Pittsburgh", "Pirates"),
        (1926, "expansion", "DET", "Detroit", "Cougars"), (1926, "expansion", "CBH", "Chicago", "Black Hawks"),
        (1931, "contraction", "PHQ", "Philadelphia", "Quakers"), (1934, "contraction", "SLE", "St. Louis", "Eagles"),
        (1938, "contraction", "MTM", "Montreal", "Maroons"), (1942, "contraction", "NYA", "New York", "Americans"),
    ]
    all_events = sorted(
        (e for e in EARLY_EVENTS + MODERN_EVENTS if e[0] > start_year),
        key=lambda x: (x[0], {"contraction": 0, "relocation": 1, "teaminfo": 1, "expansion": 2}.get(x[1], 3)),
    )
    eid = 0
    for season, kind, code, region, name in all_events:
        if kind == "expansion":
            fkey = franchise_key(code)
            if fkey not in franchise_tid:
                franchise_tid[fkey] = next_tid
                next_tid += 1
            franchise_tid[code] = franchise_tid[fkey]
            tid = franchise_tid[fkey]
            
            cid = 1 if region in {"Edmonton", "Calgary", "Vancouver", "Winnipeg", "Seattle", "San Jose", "Los Angeles", "Anaheim", "Colorado", "Dallas", "Minnesota", "St. Louis", "Nashville", "Chicago", "Detroit", "Utah", "Vegas", "Phoenix", "Denver", "Oakland", "Cleveland"} else 0
            valid_dids = [d["did"] for d in divs if d["cid"] == cid]
            did = valid_dids[0] if valid_dids else 0
            
            events.append({"type": "expansionDraft", "season": season, "phase": 4, "info": {"teams": [{"tid": tid, "region": region, "name": name, "abbrev": code, "cid": cid, "did": did, "pop": 5.0, "stadiumCapacity": 17500}]}, "id": eid})
            eid += 1
            continue
        fkey = franchise_key(code)
        if fkey not in franchise_tid: continue
        tid = franchise_tid[fkey]
        if kind == "contraction":
            events.append({"type": "contraction", "season": season, "phase": 4, "info": {"tid": tid}, "id": eid})
            eid += 1
        else:
            events.append({"type": "teamInfo", "season": season, "phase": 0, "info": {"tid": tid, "region": region, "name": name, "abbrev": code}, "id": eid})
            eid += 1
            
    merged: list[dict[str, Any]] = []
    for ev in events:
        if (merged and ev.get("type") == "expansionDraft" and merged[-1].get("type") == "expansionDraft" and merged[-1].get("season") == ev.get("season")):
            merged[-1].setdefault("info", {}).setdefault("teams", []).extend(ev.get("info", {}).get("teams", []))
        else: merged.append(ev)
    for i, ev in enumerate(merged): ev["id"] = i
    return merged

def generate(args: argparse.Namespace) -> Path:
    year = args.year
    if year < MIN_NHL_YEAR: raise SystemExit(f"NHL API data begins at {MIN_NHL_YEAR}; use a season-ending year >= {MIN_NHL_YEAR}.")
    template = safe_json_load(Path(args.template)) if args.template else None
    
    client = WebClient(args.delay)
    api = NHLAPI(client, skip_bios=args.skip_bios, skip_wikipedia=args.skip_wikipedia)
    
    print(f"Generating NHL {year - 1}-{str(year)[-2:]} historical roster")
    print("Source: Official NHL API (Position-aware rating engine)")
    
    years = [year - 1, year, year + 1]
    skaters: dict[int, list[SeasonPlayer]] = {}
    goalies: dict[int, list[GoalieSeason]] = {}
    for y in years:
        if y < MIN_NHL_YEAR: continue
        print(f"  Loading skaters {y}...")
        try:
            skaters[y] = api.load_skater_rows(y)
        except Exception as exc:
            print(f"    Warning: skater data unavailable for {y}: {exc}")
            skaters[y] = []
        print(f"    {len(skaters[y])} player-seasons")
        print(f"  Loading goalies {y}...")
        try:
            goalies[y] = api.load_goalie_rows(y)
        except Exception as exc:
            print(f"    Warning: goalie data unavailable for {y}: {exc}")
            goalies[y] = []
        print(f"    {len(goalies[y])} goalie-seasons")
        
    target_skaters = skaters.get(year, [])
    target_goalies = goalies.get(year, [])
    if not target_skaters and not target_goalies: raise RuntimeError(f"No NHL API roster data found for {year}.")
    
    print("  Loading historical teams...")
    hr_teams = api.load_teams(year, target_skaters, target_goalies)
    teams, tid_by_hr, divs, confs = make_teams(hr_teams)
    print(f"    {len(teams)} NHL teams")
    
    engine = RatingEngine(skaters, goalies)
    skater_by_year = {y: {p.name.lower(): p for p in ps} for y, ps in skaters.items()}
    goalie_by_year = {y: {p.name.lower(): p for p in ps} for y, ps in goalies.items()}
    
    bios: dict[str, Bio] = {}
    
    def fetch_bio(player: SeasonPlayer | GoalieSeason) -> tuple[str, Bio]:
        key = player.href or player.name.lower()
        return key, api.load_bio(player.href, player.name)
    
    print(f"  Fetching player bios (parallel, {args.workers} workers)...")
    all_players = target_skaters + target_goalies
    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {executor.submit(fetch_bio, p): p for p in all_players}
        completed = 0
        for future in as_completed(futures):
            key, bio = future.result()
            bios[key] = bio
            completed += 1
            if completed % 50 == 0 or completed == len(all_players):
                print(f"    {completed}/{len(all_players)} bios fetched")
        
    output_players: list[dict[str, Any]] = []
    seen_names: set[str] = set()
    draft_team_lookup = {t["abbrev"]: t["tid"] for t in teams}
    
    print("  Building skaters...")
    for p in target_skaters:
        key = p.href or p.name.lower()
        if p.name.lower() in seen_names: continue
        seen_names.add(p.name.lower())
        bio = bios.get(key, Bio(*split_name(p.name)))
        pos = get_final_position(p, bio)
        season_rating_sets: dict[int, dict[str, float]] = {}
        for y in years:
            sp = skater_by_year.get(y, {}).get(p.name.lower())
            if sp:
                if pos == "D":
                    season_rating_sets[y] = engine.defenseman_ratings(sp, y, bio)
                else:
                    season_rating_sets[y] = engine.forward_ratings(sp, y, bio, pos)
        ratings = combine_three_rating_sets(season_rating_sets.get(year - 1), season_rating_sets.get(year), season_rating_sets.get(year + 1))
        tid = tid_by_hr.get(p.team)
        if tid is None: tid = next((t["tid"] for t in teams if t["abbrev"] == normalize_team_abbrev(p.team)), 0)
        output_players.append(make_player(p.name, tid, bio, pos, ratings, draft_team_lookup))
        
    print("  Building goalies...")
    for p in target_goalies:
        key = p.href or p.name.lower()
        if p.name.lower() in seen_names: continue
        seen_names.add(p.name.lower())
        bio = bios.get(key, Bio(*split_name(p.name)))
        season_rating_sets: dict[int, dict[str, float]] = {}
        for y in years:
            gp = goalie_by_year.get(y, {}).get(p.name.lower())
            if gp: season_rating_sets[y] = engine.goalie_rating(gp, y, bio)
        ratings = combine_three_rating_sets(season_rating_sets.get(year - 1), season_rating_sets.get(year), season_rating_sets.get(year + 1))
        tid = tid_by_hr.get(p.team)
        if tid is None: tid = next((t["tid"] for t in teams if t["abbrev"] == normalize_team_abbrev(p.team)), 0)
        output_players.append(make_player(p.name, tid, bio, "G", ratings, draft_team_lookup))
        
    counts = defaultdict(int)
    for p in output_players: counts[p["tid"]] += 1
    thin = [(t["abbrev"], counts[t["tid"]]) for t in teams if counts[t["tid"]] < 24]
    if thin:
        print("  Warning: these historical teams have fewer than 24 players:")
        print("   ", ", ".join(f"{a} ({n})" for a, n in thin))
        
    season_games = [t.get("games", 0) for t in hr_teams if t.get("games", 0)]
    num_games = int(statistics.median(season_games)) if season_games else 82
    attrs = template_game_attributes(template, year, len(teams), divs, confs, num_games)
    version = template.get("version", 73) if template else 73
    
    league: dict[str, Any] = {"version": version, "startingSeason": year, "players": output_players, "teams": teams, "gameAttributes": attrs}
    if not args.no_scheduled_events:
        print("  Building scheduled historical expansion/relocation events...")
        events = make_scheduled_events(year, {t["hr_code"] for t in hr_teams}, tid_by_hr, divs)
        league["scheduledEvents"] = events
        print(f"    {len(league['scheduledEvents'])} scheduled events")
        
    out = Path(args.output or f"{year}_zenGM_roster.json")
    with out.open("w", encoding="utf-8") as f: json.dump(league, f, ensure_ascii=False, indent=2)
    print("\nDONE")
    print(f"  Players: {len(output_players)}")
    print(f"  Teams:   {len(teams)}")
    print(f"  Output:  {out.resolve()}")
    print("  Import:  ZenGM Hockey -> New League -> Customize -> Upload league file")
    return out

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Generate a historical NHL ZenGM Hockey roster from the NHL API.")
    p.add_argument("year", type=int, help="Season-ending year, e.g. 1987 for 1986-87")
    p.add_argument("--template", help="Existing ZenGM Hockey league export (.json or .json.gz)")
    p.add_argument("--output", help="Output JSON filename")
    p.add_argument("--delay", type=float, default=DEFAULT_DELAY, help="Seconds between uncached web requests")
    p.add_argument("--workers", type=int, default=8, help="Number of parallel workers for bio fetching")
    p.add_argument("--skip-bios", action="store_true", help="Skip fetching player bios entirely")
    p.add_argument("--skip-wikipedia", action="store_true", help="Skip Wikipedia fallback")
    p.add_argument("--no-scheduled-events", action="store_true", help="Do not add historical events")
    return p

def main() -> None:
    args = build_parser().parse_args()
    if not args.template:
        for candidate in (Path("ZGMH_League_1_2026_preseason.json.gz"), Path("ZGMH_League_1_2026_preseason.json")):
            if candidate.exists():
                args.template = str(candidate)
                print(f"Using ZenGM template: {candidate}")
                break
    try:
        generate(args)
    except KeyboardInterrupt:
        print("\nStopped.")
        sys.exit(130)
    except Exception as exc:
        print(f"\nERROR: {exc}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        sys.exit(1)

if __name__ == "__main__":
    main()
