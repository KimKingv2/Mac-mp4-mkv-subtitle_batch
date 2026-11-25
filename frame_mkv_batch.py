#!/usr/bin/env python3
# FRAME Cinema MKV Batch Muxer for macOS
# v2.4.14: export the identifiable Chinese/bilingual subtitle track as a same-basename .zh sidecar alongside EN/JA/KO.
# Audio/subtitles/chapters/attachments are kept via MKVToolNix remux; source files are never modified.

import json
import hashlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
import ctypes
import errno
import copy
from collections import defaultdict, Counter
from pathlib import Path
from datetime import datetime
from fractions import Fraction

VERSION = "2.4.14-beta"
VIDEO_EXTS = {".mkv", ".mp4", ".m4v"}
SUB_EXTS = {".ass", ".ssa", ".srt"}
EP_RE = re.compile(r"(?i)(?<![A-Z0-9])S(\d{1,2})[ ._-]*E(\d{1,3})(?![A-Z0-9])")

LANGUAGE_PROFILES = {
    "1": {"code": "eng", "ietf": "en", "name": "English", "suffix": ".en"},
    "2": {"code": "jpn", "ietf": "ja", "name": "Japanese", "suffix": ".ja"},
    "3": {"code": "kor", "ietf": "ko", "name": "Korean", "suffix": ".ko"},
}
ALLOWED_AUDIO_LANGUAGES = {"eng", "jpn", "kor"}


def write_log(logfile, message, also_print=True):
    message = str(message)
    if also_print:
        print(message, flush=True)
    if logfile:
        with logfile.open("a", encoding="utf-8") as f:
            f.write(message + "\n")
            f.flush()


def write_block(logfile, title, content):
    content = (content or "").strip()
    if not content:
        return
    with logfile.open("a", encoding="utf-8") as f:
        f.write("\n--- {} ---\n{}\n--- END {} ---\n".format(title, content, title))
        f.flush()


def choose_folder(prompt):
    # The AppleScript prompt is static text; no user-provided string is interpolated.
    script = 'POSIX path of (choose folder with prompt "' + prompt + '")'
    result = subprocess.run(
        ["/usr/bin/osascript", "-e", script],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", errors="replace"
    )
    if result.returncode != 0:
        raise RuntimeError("已取消選擇資料夾。")
    p = Path(result.stdout.strip()).resolve()
    if not p.is_dir():
        raise RuntimeError("選擇的路徑不是資料夾：" + str(p))
    return p


def choose_file(prompt):
    # Static AppleScript text; the user selects the file in the macOS picker.
    script = 'POSIX path of (choose file with prompt "' + prompt + '")'
    result = subprocess.run(
        ["/usr/bin/osascript", "-e", script],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", errors="replace"
    )
    if result.returncode != 0:
        raise RuntimeError("已取消選擇影片檔案。")
    p = Path(result.stdout.strip()).resolve()
    if not p.is_file():
        raise RuntimeError("選擇的路徑不是檔案：" + str(p))
    return p


def find_tool(name, candidates=()):
    found = shutil.which(name)
    if found:
        return str(Path(found).resolve())
    for p in candidates:
        if p.is_file() and os.access(str(p), os.X_OK):
            return str(p)
    return None


def find_mkvmerge():
    candidates = [
        Path("/opt/homebrew/bin/mkvmerge"),
        Path("/usr/local/bin/mkvmerge"),
        Path("/Applications/MKVToolNix.app/Contents/MacOS/mkvmerge"),
    ]
    app_dir = Path("/Applications")
    if app_dir.exists():
        candidates.extend(app_dir.glob("MKVToolNix*.app/Contents/MacOS/mkvmerge"))
    return find_tool("mkvmerge", candidates)


def find_mkvextract(mkvmerge=None):
    candidates = [
        Path("/opt/homebrew/bin/mkvextract"),
        Path("/usr/local/bin/mkvextract"),
        Path("/Applications/MKVToolNix.app/Contents/MacOS/mkvextract"),
    ]
    if mkvmerge:
        sibling = Path(mkvmerge).with_name("mkvextract")
        candidates.insert(0, sibling)
    app_dir = Path("/Applications")
    if app_dir.exists():
        candidates.extend(app_dir.glob("MKVToolNix*.app/Contents/MacOS/mkvextract"))
    return find_tool("mkvextract", candidates)


def find_ffmpeg():
    return find_tool("ffmpeg", [Path("/opt/homebrew/bin/ffmpeg"), Path("/usr/local/bin/ffmpeg")])


def find_ffprobe():
    return find_tool("ffprobe", [Path("/opt/homebrew/bin/ffprobe"), Path("/usr/local/bin/ffprobe")])


def run_text(cmd):
    return subprocess.run(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", errors="replace"
    )


def episode_key(path):
    path = Path(path)
    # Directory names have no semantic extension; Path.stem would incorrectly
    # strip a final token such as ".S01E01" from an episode folder.
    name = path.name if path.is_dir() else path.stem
    matches = list(EP_RE.finditer(name))
    if len(matches) != 1:
        return None
    m = matches[0]
    tail = name[m.end():]
    if re.match(r"(?i)[ ._-]*E\d{1,3}(?!\d)", tail):
        return None
    return "S{:02d}E{:02d}".format(int(m.group(1)), int(m.group(2)))


def collect(root, exts):
    items = []
    for p in root.rglob("*"):
        try:
            if p.is_file() and p.suffix.lower() in exts:
                items.append(p)
        except OSError:
            pass
    return sorted(items, key=lambda x: str(x).casefold())


def index_by_episode(paths):
    idx = defaultdict(list)
    untagged = []
    for p in paths:
        key = episode_key(p)
        if key:
            idx[key].append(p)
        else:
            untagged.append(p)
    return idx, untagged


def _normalise_release_folder_name(value):
    """Normalise only case and repeated whitespace; preserve release identity punctuation."""
    return re.sub(r"\s+", " ", str(value or "").strip()).casefold()


def find_rarbg_episode_folder(video_path, subs_root):
    """Find the per-episode RARBG Subs directory matching a video release.

    Prefer an exact directory-name == video stem match. Fall back to a unique
    SxxEyy directory match only when the exact release folder is absent.
    """
    video_path = Path(video_path)
    subs_root = Path(subs_root)
    if not subs_root.is_dir():
        raise RuntimeError("RARBG Subs 路徑不是資料夾：{}".format(subs_root))

    wanted = _normalise_release_folder_name(video_path.stem)
    exact = [p for p in subs_root.iterdir() if p.is_dir()
             and _normalise_release_folder_name(p.name) == wanted]
    if len(exact) == 1:
        return exact[0], "依影片完整檔名精確匹配字幕資料夾。"
    if len(exact) > 1:
        raise RuntimeError("Subs 中存在多個與影片檔名完全相同的資料夾：{}".format(
            ", ".join(str(x) for x in exact)))

    key = episode_key(video_path)
    if not key:
        raise RuntimeError("影片檔名沒有唯一 SxxExx 編號，無法匹配 RARBG Subs 資料夾。")
    candidates = []
    for folder in subs_root.iterdir():
        if folder.is_dir() and episode_key(folder) == key:
            candidates.append(folder)
    if len(candidates) == 1:
        return candidates[0], "完整檔名資料夾不存在；依唯一 SxxExx 資料夾匹配。"
    if not candidates:
        raise RuntimeError("找不到對應的 RARBG Subs 子資料夾（影片：{}；集數：{}）。".format(
            video_path.name, key))
    raise RuntimeError("依 {} 找到多個字幕資料夾，無法安全選擇：{}".format(
        key, ", ".join(x.name for x in candidates)))


def _read_subtitle_text_for_classification(path):
    """Read text subtitle contents for SDH classification; never modifies the file."""
    path = Path(path)
    raw = path.read_bytes()
    for encoding in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def classify_english_subtitle(path):
    """Classify an English subtitle as standard, SDH, forced, or uncertain.

    Explicit SDH markers have priority. Otherwise, a sufficiently high density
    of standalone sound-description cues is treated as SDH. Filename numbering
    (3_, 4_, etc.) is deliberately not used as semantic evidence.
    """
    path = Path(path)
    name = path.stem.casefold()
    if re.search(r"(?:^|[ ._-])forced(?:$|[ ._-])", name):
        return {"kind": "forced", "reason": "檔名明確標示 Forced。", "cue_count": 0}

    text = _read_subtitle_text_for_classification(path)
    text_low = text.casefold()
    explicit_patterns = (
        r"\b sdh \b", r"hearing[ _-]*impaired", r"closed[ _-]*captions?",
        r"\[\s*english\s*[-–—]\s*[a-z]{2}(?:[-_][a-z]{2})?\s*[-–—]\s*sdh\s*\]",
        r"\[\s*english\s*[-–—]\s*us\s*[-–—]\s*sdh\s*\]",
    )
    explicit_sdh = any(re.search(pattern, text_low, re.IGNORECASE | re.VERBOSE)
                       for pattern in explicit_patterns)
    filename_sdh = bool(re.search(r"(?:^|[ ._-])(?:sdh|hi|cc)(?:$|[ ._-])", name))

    event_count = len(re.findall(r"(?m)^\s*\d+\s*$", text))
    if event_count and event_count <= 20:
        return {"kind": "forced", "reason": "字幕事件只有 {} 條，視為可能的 Forced／局部字幕，不納入完整英文字幕配對。".format(
            event_count), "cue_count": 0, "event_count": event_count}

    cue_lines = []
    for raw_line in text.splitlines():
        line = raw_line.strip().lstrip("\ufeff")
        if not line or re.fullmatch(r"\d+", line):
            continue
        if re.match(r"\d{1,2}:\d{2}:\d{2}[,.]\d{3}\s*-->\s*", line):
            continue
        if re.fullmatch(r"\[[^\]]{2,100}\]", line) or re.fullmatch(r"\([^)]{2,100}\)", line):
            cue_lines.append(line)
        elif "♪" in line or "♫" in line:
            cue_lines.append(line)

    if explicit_sdh or filename_sdh:
        reason = "找到明確 SDH／聽障字幕標記。" if explicit_sdh else "檔名明確標示 SDH。"
        return {"kind": "sdh", "reason": reason, "cue_count": len(cue_lines), "event_count": event_count}

    # Avoid classifying ordinary subtitles from one or two incidental cues.
    cue_ratio = len(cue_lines) / max(event_count, 1)
    if len(cue_lines) >= 5 and cue_ratio >= 0.01:
        return {"kind": "sdh", "reason": "未找到 SDH 標籤，但聲音描述行達到 SDH 判定門檻（{} 行／{:.1%}）。".format(
            len(cue_lines), cue_ratio), "cue_count": len(cue_lines), "event_count": event_count}

    return {"kind": "standard", "reason": "未發現 SDH 標記或足量聲音描述；列為普通英文候選。", 
            "cue_count": len(cue_lines), "event_count": event_count}


def select_rarbg_english_and_sdh(video_path, subs_root):
    """Return (standard_English, English_SDH, diagnostic) for a RARBG episode."""
    folder, folder_reason = find_rarbg_episode_folder(video_path, subs_root)
    candidates = []
    ignored = []
    for item in collect(folder, SUB_EXTS):
        if not re.search(r"(?i)(?:^|[ ._-])english(?:$|[ ._-])", item.stem):
            continue
        try:
            info = classify_english_subtitle(item)
        except Exception as exc:
            ignored.append("{}（讀取失敗：{}）".format(item.name, exc))
            continue
        info["path"] = item
        if info["kind"] == "forced":
            ignored.append("{}（Forced／短字幕候選，不納入完整英文／SDH 配對：{}）".format(
                item.name, info["reason"]))
        else:
            candidates.append(info)

    standard = [x for x in candidates if x["kind"] == "standard"]
    sdh = [x for x in candidates if x["kind"] == "sdh"]
    if len(standard) != 1 or len(sdh) != 1:
        details = ["字幕資料夾：{}".format(folder),
                   "英文候選：{}；普通英文：{}；SDH：{}。".format(
                       len(candidates), len(standard), len(sdh))]
        for item in candidates:
            details.append("{}：{}（字幕事件 {}；聲音描述 {} 行）".format(
                item["path"].name, item["reason"], item.get("event_count", "未知"), item["cue_count"]))
        if ignored:
            details.append("忽略：" + "；".join(ignored))
        raise RuntimeError("無法唯一識別一份普通英文字幕及一份 SDH 字幕。\n" + "\n".join(details))

    diagnostic = "{}；普通英文：{}（{}）；SDH：{}（{}）。".format(
        folder_reason, standard[0]["path"].name, standard[0]["reason"],
        sdh[0]["path"].name, sdh[0]["reason"])
    if ignored:
        diagnostic += " 忽略：" + "；".join(ignored)
    return standard[0]["path"], sdh[0]["path"], diagnostic


def select_rarbg_standard_english(video_path, subs_root):
    """Select the unique ordinary English track from one RARBG Subs episode folder.

    SDH and Forced tracks are classified but not muxed by the English+Chinese mode.
    File numbering such as 3_ / 4_ is not used as semantic evidence.
    """
    folder, folder_reason = find_rarbg_episode_folder(video_path, subs_root)
    standard, sdh, forced, unreadable = [], [], [], []
    for item in collect(folder, SUB_EXTS):
        if not re.search(r"(?i)(?:^|[ ._-])english(?:$|[ ._-])", item.stem):
            continue
        try:
            info = classify_english_subtitle(item)
        except Exception as exc:
            unreadable.append("{}（讀取失敗：{}）".format(item.name, exc))
            continue
        entry = (item, info)
        if info["kind"] == "standard":
            standard.append(entry)
        elif info["kind"] == "sdh":
            sdh.append(entry)
        else:
            forced.append(entry)

    if len(standard) != 1 or unreadable:
        details = ["字幕資料夾：{}".format(folder),
                   "普通英文候選：{}；SDH：{}；Forced／局部字幕：{}。".format(
                       len(standard), len(sdh), len(forced))]
        for item, info in standard + sdh + forced:
            details.append("{}：{}（字幕事件 {}；聲音描述 {} 行）".format(
                item.name, info.get("reason", ""), info.get("event_count", "未知"),
                info.get("cue_count", 0)))
        if unreadable:
            details.extend(unreadable)
        raise RuntimeError("無法唯一識別一份普通英文字幕。\n" + "\n".join(details))

    selected, info = standard[0]
    diagnostic = "{}；選用普通英文：{}（{}）。".format(
        folder_reason, selected.name, info["reason"])
    if sdh:
        diagnostic += " 偵測到但本模式不封裝 SDH：{}。".format(
            ", ".join(x.name for x, _ in sdh))
    if forced:
        diagnostic += " 忽略 Forced／局部字幕：{}。".format(
            ", ".join(x.name for x, _ in forced))
    return selected, diagnostic


def existing_english_sdh_sidecar_paths(output):
    """List possible SDH sidecar targets so preflight never overwrites them."""
    base = output.parent / (output.stem + ".en.sdh")
    extensions = (".ass", ".ssa", ".srt", ".usf", ".vtt", ".sup", ".idx", ".sub")
    return [Path(str(base) + ext) for ext in extensions if Path(str(base) + ext).exists()]


def is_within(child, parent):
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False


def existing_english_sidecar_paths(output):
    """List possible English sidecar targets so the whole job can be skipped preflight."""
    base = output.parent / (output.stem + ".en")
    extensions = (".ass", ".ssa", ".srt", ".usf", ".vtt", ".sup", ".idx", ".sub")
    return [Path(str(base) + ext) for ext in extensions if Path(str(base) + ext).exists()]


def existing_language_sidecar_paths(output, target_language="eng"):
    profile = next((v for v in LANGUAGE_PROFILES.values() if v["code"] == target_language),
                   LANGUAGE_PROFILES["1"])
    base = output.parent / (output.stem + profile["suffix"])
    extensions = (".ass", ".ssa", ".srt", ".usf", ".vtt", ".sup", ".idx", ".sub")
    return [Path(str(base) + ext) for ext in extensions if Path(str(base) + ext).exists()]


def existing_chinese_sidecar_paths(output):
    """Find legacy and locale-specific Chinese/bilingual sidecars before processing."""
    suffixes = (
        ".zh", ".zh-tw", ".zh-cn",
        ".zh-tw-en", ".zh-tw-ja", ".zh-tw-ko",
        ".zh-cn-en", ".zh-cn-ja", ".zh-cn-ko",
    )
    extensions = (".ass", ".ssa", ".srt", ".usf", ".vtt", ".sup", ".idx", ".sub")
    found = []
    for suffix in suffixes:
        base = output.parent / (output.stem + suffix)
        found.extend(Path(str(base) + ext) for ext in extensions if Path(str(base) + ext).exists())
    return found



def normalize_ietf_language_tag(value):
    """Normalize a BCP-47 tag for case-insensitive equality checks."""
    return str(value or "").strip().replace("_", "-").lower()


def identify_bilingual_subtitle_profile(subtitle_path, primary_language="eng"):
    """Identify a Chinese+original-language sidecar from an explicit path marker.

    Accepted Chinese variant markers:
      zh-TW / zh-Hant / Traditional Chinese / 繁體中文
      zh-CN / zh-Hans / Simplified Chinese / 簡體中文

    An optional original-language suffix (EN/JA/KO) must agree with the selected
    primary-language profile. The original language is never guessed from subtitle
    text; when no suffix exists, the selected batch profile is used.
    """
    profile = next((item for item in LANGUAGE_PROFILES.values()
                    if item["code"] == primary_language), None)
    if profile is None:
        raise RuntimeError("所選主要語言 {} 不在 English／Japanese／Korean 支援範圍。".format(
            primary_language))

    raw_path = str(subtitle_path).replace("\\", "/")
    normalized = re.sub(r"[_\s]+", "-", raw_path.casefold())
    locale_candidates = set()
    for match in re.finditer(r"(?<![a-z0-9])zh-(tw|hant|cn|hans)(?![a-z])", normalized):
        token = match.group(1)
        locale_candidates.add("zh-TW" if token in {"tw", "hant"} else "zh-CN")
    if re.search(r"繁體(?:中文)?|繁体(?:中文)?|traditional[-_ ]*chinese", raw_path, re.IGNORECASE):
        locale_candidates.add("zh-TW")
    if re.search(r"簡體(?:中文)?|简体(?:中文)?|simplified[-_ ]*chinese", raw_path, re.IGNORECASE):
        locale_candidates.add("zh-CN")

    if not locale_candidates:
        raise RuntimeError(
            "雙語字幕路徑未標明簡體或繁體，無法安全設定 zh-CN／zh-TW。"
            "請在字幕資料夾或檔名加入 zh-CN_EN／zh-TW_EN，"
            "或 Simplified Chinese／Traditional Chinese 標記。"
        )
    if len(locale_candidates) != 1:
        raise RuntimeError("雙語字幕路徑同時包含簡體與繁體標記，無法唯一判定：{}".format(
            ", ".join(sorted(locale_candidates))))

    language_aliases = {
        "en": "eng", "eng": "eng", "english": "eng",
        "ja": "jpn", "jpn": "jpn", "japanese": "jpn",
        "ko": "kor", "kor": "kor", "korean": "kor",
    }
    explicit_originals = set()
    for match in re.finditer(
        r"(?<![a-z0-9])zh-(?:tw|hant|cn|hans)-([a-z]{2,9})(?![a-z])", normalized
    ):
        token = match.group(1)
        mapped = language_aliases.get(token)
        if mapped is None:
            raise RuntimeError("雙語字幕語系標記包含不支援的原語言 {!r}；目前只支援 EN／JA／KO。".format(
                token))
        explicit_originals.add(mapped)

    if len(explicit_originals) > 1:
        raise RuntimeError("雙語字幕路徑包含多個原語言標記，無法唯一判定。")
    if explicit_originals and primary_language not in explicit_originals:
        found = next(iter(explicit_originals))
        raise RuntimeError("雙語字幕標記的原語言為 {}，但本批主要字幕設定為 {}；拒絕錯配。".format(
            found, primary_language))

    locale = next(iter(locale_candidates))
    chinese_name = "繁體中文" if locale == "zh-TW" else "簡體中文"
    track_name = "{} + {}".format(chinese_name, profile["name"])
    original_suffix = {"eng": "en", "jpn": "ja", "kor": "ko"}[primary_language]
    source_note = ("路徑明確標記原語言 {}".format(next(iter(explicit_originals)))
                   if explicit_originals else "原語言依本批主要語言設定 {}".format(primary_language))
    return {
        "language_ietf": locale,
        "track_name": track_name,
        "chinese_name": chinese_name,
        "original_language": primary_language,
        "original_language_name": profile["name"],
        "sidecar_suffix": ".{}-{}".format(locale.lower(), original_suffix),
        "source_note": source_note,
    }


def infer_bilingual_original_language(track_name):
    """Return EN/JA/KO only when a bilingual track title names exactly one original language."""
    name = str(track_name or "").casefold()
    patterns = {
        "eng": r"(?<![a-z])(?:english|eng)(?![a-z])",
        "jpn": r"(?<![a-z])(?:japanese|jpn|日本語|日文|日語)",
        "kor": r"(?<![a-z])(?:korean|kor|한국어|韓國語|韓文|韓語)",
    }
    matches = [code for code, pattern in patterns.items() if re.search(pattern, name)]
    return matches[0] if len(matches) == 1 else None


def chinese_sidecar_suffix(track):
    """Choose a descriptive sidecar suffix when locale and original language are explicit."""
    props = track.get("properties", {}) or {}
    ietf = normalize_ietf_language_tag(props.get("language_ietf"))
    name = str(props.get("track_name") or "")
    locale = {"zh-tw": "zh-tw", "zh-cn": "zh-cn"}.get(ietf)
    if locale:
        original = infer_bilingual_original_language(name)
        if original and ("+" in name or "＋" in name):
            suffix_code = {"eng": "en", "jpn": "ja", "kor": "ko"}[original]
            return ".{}-{}".format(locale, suffix_code)
        return ".{}".format(locale)
    # Preserve the v2.4.14 .zh convention for older or generic Chinese tracks.
    return ".zh"


def ffprobe_info(ffprobe, path):
    cmd = [
        ffprobe, "-v", "error", "-show_streams", "-show_format",
        "-of", "json", str(path)
    ]
    proc = run_text(cmd)
    if proc.returncode != 0:
        raise RuntimeError("ffprobe 退出碼 {}：{}".format(proc.returncode, (proc.stderr or proc.stdout)[-3000:]))
    try:
        data = json.loads(proc.stdout)
    except Exception as exc:
        raise RuntimeError("無法解析 ffprobe 資訊：{}；{}".format(exc, proc.stdout[-1000:]))
    return data, proc.stderr


def mkvmerge_info(mkvmerge, path):
    proc = run_text([mkvmerge, "-J", str(path)])
    if proc.returncode not in (0, 1):
        raise RuntimeError("mkvmerge -J 退出碼 {}：{}".format(proc.returncode, (proc.stderr or proc.stdout)[-3000:]))
    try:
        data = json.loads(proc.stdout)
    except Exception as exc:
        raise RuntimeError("無法解析 mkvmerge -J 資訊：{}；{}".format(exc, proc.stdout[-1000:]))
    return data, proc.returncode, proc.stderr


def canonical_language_code(value):
    """Return ISO-639-2 language code (eng/jpn/kor) for allowed audio/subtitle languages.

    Other explicit languages are returned as their normalized primary tag. Unknown/und
    values return None. This deliberately avoids treating an unknown track as English.
    """
    raw = str(value or "").strip().replace("_", "-").lower()
    if not raw or raw in {"und", "unknown", "zxx", "mis"}:
        return None
    primary = raw.split("-", 1)[0]
    # Normalize common ISO-639-1 / ISO-639-2 pairs so e.g. Language=rus and
    # LanguageIETF=ru-RU are recognized as the same language, not a conflict.
    aliases = {
        "en": "eng", "eng": "eng", "english": "eng",
        "ja": "jpn", "jp": "jpn", "jpn": "jpn", "japanese": "jpn",
        "ko": "kor", "kr": "kor", "kor": "kor", "korean": "kor",
        "ru": "rus", "rus": "rus",
        "zh": "zho", "chi": "zho", "zho": "zho",
        "fr": "fra", "fre": "fra", "fra": "fra",
        "de": "deu", "ger": "deu", "deu": "deu",
        "es": "spa", "spa": "spa",
        "it": "ita", "ita": "ita",
        "pt": "por", "por": "por",
        "ar": "ara", "ara": "ara",
        "nl": "nld", "dut": "nld", "nld": "nld",
        "sv": "swe", "swe": "swe",
        "da": "dan", "dan": "dan",
        "no": "nor", "nor": "nor",
        "fi": "fin", "fin": "fin",
        "pl": "pol", "pol": "pol",
        "tr": "tur", "tur": "tur",
        "el": "ell", "gre": "ell", "ell": "ell",
        "he": "heb", "heb": "heb",
        "th": "tha", "tha": "tha",
        "vi": "vie", "vie": "vie",
        "uk": "ukr", "ukr": "ukr",
        "hi": "hin", "hin": "hin",
        "id": "ind", "ind": "ind",
        "ms": "msa", "may": "msa", "msa": "msa",
        "cs": "ces", "cze": "ces", "ces": "ces",
        "hu": "hun", "hun": "hun",
        "ro": "ron", "rum": "ron", "ron": "ron",
        "bg": "bul", "bul": "bul",
        "hr": "hrv", "hrv": "hrv",
        "sr": "srp", "srp": "srp",
        "sk": "slk", "slo": "slk", "slk": "slk",
        "sl": "slv", "slv": "slv",
        "et": "est", "est": "est",
        "lv": "lav", "lav": "lav",
        "lt": "lit", "lit": "lit",
        "fa": "fas", "per": "fas", "fas": "fas",
        "ur": "urd", "urd": "urd",
        "bn": "ben", "ben": "ben",
        "ta": "tam", "tam": "tam",
        "te": "tel", "tel": "tel",
        "sw": "swa", "swa": "swa",
        "is": "isl", "ice": "isl", "isl": "isl",
    }
    return aliases.get(primary, primary)


def infer_language_from_track_name(track_name):
    """Infer only clear language names from a track title; return None if ambiguous."""
    name = str(track_name or "").casefold()
    matches = []
    patterns = (
        ("eng", r"(?<![a-z])(?:english|eng)(?![a-z])"),
        ("jpn", r"(?<![a-z])(?:japanese|japan|jpn)(?![a-z])"),
        ("kor", r"(?<![a-z])(?:korean|korea|kor)(?![a-z])"),
        ("zho", r"中文|普通话|普通話|国语|國語|粤语|粵語|chinese|mandarin|cantonese|(?<![a-z])(?:zho|chi)(?![a-z])"),
        ("rus", r"(?<![a-z])(?:russian|rus)(?![a-z])"),
        ("fra", r"(?<![a-z])(?:french|fra|fre)(?![a-z])"),
        ("deu", r"(?<![a-z])(?:german|deu|ger)(?![a-z])"),
        ("spa", r"(?<![a-z])(?:spanish|spa)(?![a-z])"),
        ("ita", r"(?<![a-z])(?:italian|ita)(?![a-z])"),
        ("por", r"(?<![a-z])(?:portuguese|por)(?![a-z])"),
        ("ara", r"(?<![a-z])(?:arabic|ara)(?![a-z])"),
        ("nld", r"(?<![a-z])(?:dutch|nld|dut)(?![a-z])"),
        ("swe", r"(?<![a-z])(?:swedish|swe)(?![a-z])"),
        ("dan", r"(?<![a-z])(?:danish|dan)(?![a-z])"),
        ("nor", r"(?<![a-z])(?:norwegian|nor)(?![a-z])"),
        ("fin", r"(?<![a-z])(?:finnish|fin)(?![a-z])"),
    )
    for code, pattern in patterns:
        if re.search(pattern, name):
            matches.append(code)
    return matches[0] if len(matches) == 1 else None


def resolve_track_language(properties):
    """Resolve track language using IETF, legacy language, then an unambiguous title."""
    props = properties or {}
    ietf_raw = props.get("language_ietf")
    legacy_raw = props.get("language")
    ietf = canonical_language_code(ietf_raw)
    legacy = canonical_language_code(legacy_raw)
    if ietf and legacy and ietf != legacy:
        raise RuntimeError("LanguageIETF 與 Language 語言標籤互相衝突（{} / {}）。".format(
            ietf_raw, legacy_raw))
    if ietf:
        return ietf, "LanguageIETF={!r}".format(ietf_raw)
    if legacy:
        return legacy, "Language={!r}".format(legacy_raw)
    inferred = infer_language_from_track_name(props.get("track_name"))
    if inferred:
        return inferred, "依明確音軌名稱 {!r} 推斷".format(props.get("track_name"))
    return None, "缺少可識別語言標籤／名稱"


def build_audio_language_plan(mkv_info, preferred_language="eng"):
    """Keep only English/Japanese/Korean audio. Unknown tracks fail closed.

    Original default flags are preserved for retained tracks. If all original defaults
    were filtered out, select the preferred-language track, otherwise the first retained
    track, as the single fallback default so the output is not left without one.
    """
    tracks = _mkv_tracks(mkv_info, "audio")
    if not tracks:
        raise RuntimeError("來源沒有音訊軌，無法套用英／日／韓音訊保留規則。")
    kept, dropped, unresolved = [], [], []
    for track in tracks:
        props = track.get("properties", {}) or {}
        lang, reason = resolve_track_language(props)
        if lang is None:
            unresolved.append("Track ID {} 名稱={!r} Language={!r} LanguageIETF={!r}".format(
                track.get("id"), props.get("track_name"), props.get("language"), props.get("language_ietf")))
            continue
        if lang in ALLOWED_AUDIO_LANGUAGES:
            if track.get("id") is None:
                raise RuntimeError("可保留的音訊軌缺少 MKVToolNix Track ID，無法安全篩選。")
            kept.append((track, lang, reason))
        else:
            dropped.append((track, lang, reason))
    if unresolved:
        raise RuntimeError(
            "有音訊軌的語言無法可靠識別，為避免誤刪英／日／韓音訊，停止此集。請先在來源檔補正音軌語言標籤：\n" +
            "\n".join(unresolved))
    if not kept:
        raise RuntimeError("來源沒有可識別的 English／Japanese／Korean 音訊軌；停止，不產生無音訊輸出。")

    selected_ids = [int(item[0]["id"]) for item in kept]
    default_ids = [int(item[0]["id"]) for item in kept
                   if (item[0].get("properties", {}) or {}).get("default_track") is True]
    if not default_ids:
        preferred = [int(track["id"]) for track, lang, _ in kept if lang == preferred_language]
        default_ids = preferred[:1] or selected_ids[:1]
    return {
        "selected_ids": selected_ids,
        "kept_tracks": [item[0] for item in kept],
        "kept_languages": [item[1] for item in kept],
        "dropped": [(item[0], item[1]) for item in dropped],
        "default_ids": default_ids,
    }


def audio_language_cli_options(plan):
    """mkvmerge input options for the allowlisted audio tracks and retained defaults."""
    args = ["--audio-tracks", ",".join(str(x) for x in plan["selected_ids"])]
    for track_id in plan["selected_ids"]:
        args.extend(["--default-track-flag", "{}:{}".format(
            track_id, "yes" if track_id in plan["default_ids"] else "no")])
    return args


def filtered_source_views(source_probe, source_mkv, audio_plan):
    """Create verification snapshots representing only the retained source audio tracks."""
    selected_ids = set(audio_plan["selected_ids"])
    original_mkv_audio = _mkv_tracks(source_mkv, "audio")
    original_probe_audio = [s for s in source_probe.get("streams", []) if s.get("codec_type") == "audio"]
    if len(original_mkv_audio) != len(original_probe_audio):
        raise RuntimeError("MKVToolNix 與 FFprobe 對來源音訊軌數量的辨識不一致，無法安全篩選。")
    kept_ordinals = [i for i, track in enumerate(original_mkv_audio) if int(track.get("id", -1)) in selected_ids]
    kept_stream_indices = []
    for ordinal, (mkv_track, probe_stream) in enumerate(zip(original_mkv_audio, original_probe_audio)):
        mkv_lang, _ = resolve_track_language(mkv_track.get("properties", {}) or {})
        probe_tags = probe_stream.get("tags", {}) or {}
        probe_lang = canonical_language_code(probe_tags.get("language"))
        # FFprobe may omit a language tag that MKVToolNix can read from another
        # Matroska element; only reject an explicit, contradictory FFprobe tag.
        if mkv_lang and probe_lang and mkv_lang != probe_lang:
            raise RuntimeError(
                "MKVToolNix 與 FFprobe 的第 {} 條來源音訊語言不一致（{} / {}）；"
                "無法安全依軌道順序篩選。請先檢查來源檔語言標籤。".format(
                    ordinal + 1, mkv_lang, probe_lang))
    for ordinal in kept_ordinals:
        stream = original_probe_audio[ordinal]
        if stream.get("index") is None:
            raise RuntimeError("FFprobe 來源音訊串流缺少 index，無法驗證篩選後封包。")
        kept_stream_indices.append(int(stream["index"]))

    mkv_view = copy.deepcopy(source_mkv)
    mkv_view["tracks"] = [t for t in source_mkv.get("tracks", [])
                          if t.get("type") != "audio" or int(t.get("id", -1)) in selected_ids]
    default_ids = set(audio_plan["default_ids"])
    for track in _mkv_tracks(mkv_view, "audio"):
        track.setdefault("properties", {})["default_track"] = int(track.get("id", -1)) in default_ids
    probe_view = copy.deepcopy(source_probe)
    audio_ordinal = 0
    filtered_streams = []
    for stream in source_probe.get("streams", []):
        if stream.get("codec_type") == "audio":
            if audio_ordinal in kept_ordinals:
                filtered_streams.append(stream)
            audio_ordinal += 1
        else:
            filtered_streams.append(stream)
    probe_view["streams"] = filtered_streams
    selected_ordinal = 0
    for stream in probe_view["streams"]:
        if stream.get("codec_type") != "audio":
            continue
        track = audio_plan["kept_tracks"][selected_ordinal]
        track_id = int(track.get("id"))
        disposition = dict(stream.get("disposition", {}) or {})
        disposition["default"] = 1 if track_id in default_ids else 0
        stream["disposition"] = disposition
        selected_ordinal += 1
    probe_view["_mkvmerge_info"] = mkv_view
    probe_view["_mkvmerge_tracks"] = mkv_view.get("tracks", [])
    probe_view["_audio_source_stream_indices"] = kept_stream_indices
    return probe_view, mkv_view


def log_audio_language_plan(logfile, plan):
    write_log(logfile, "音訊保留規則：只保留 English／Japanese／Korean（eng／jpn／kor）。")
    for track in plan["kept_tracks"]:
        props = track.get("properties", {}) or {}
        lang, reason = resolve_track_language(props)
        write_log(logfile, "保留音訊 Track ID {}：{}；名稱={!r}；{}。{}。".format(
            track.get("id"), lang, props.get("track_name"), reason,
            "Default Yes" if int(track.get("id")) in plan["default_ids"] else "Default No"))
    for track, lang in plan["dropped"]:
        props = track.get("properties", {}) or {}
        write_log(logfile, "排除非英／日／韓音訊 Track ID {}：{}；名稱={!r}。".format(
            track.get("id"), lang, props.get("track_name")))
    write_log(logfile, "輸出音訊軌數：{}；保留語言：{}。".format(
        len(plan["kept_tracks"]), ", ".join(plan["kept_languages"])))


def stream_counts(info):
    streams = info.get("streams", [])
    return Counter(s.get("codec_type") for s in streams if s.get("codec_type") in ("video", "audio", "subtitle"))


def mkv_track_counts(info):
    return Counter(t.get("type") for t in info.get("tracks", []) if t.get("type") in ("video", "audio", "subtitles"))


def stream_codec_counts(info, kind):
    return Counter(
        s.get("codec_name", "unknown")
        for s in info.get("streams", [])
        if s.get("codec_type") == kind
    )


def compare_track_property(source_props, output_props, key):
    """Compare protected track metadata while allowing MKVToolNix's documented IETF backfill.

    Matroska has a legacy ISO-639-2 Language field and a newer BCP-47 LanguageIETF field.
    If a source track has no explicit LanguageIETF element, mkvmerge may create one from
    the unchanged legacy Language value when writing the remux. That is metadata completion,
    not a change to the source track's selected language. The legacy Language field is
    compared strictly elsewhere in the same validation loop.

    Returns (matches, note). Explicit source LanguageIETF values remain protected: if one
    changes or disappears, validation still fails.
    """
    source_value = source_props.get(key)
    output_value = output_props.get(key)
    if source_value == output_value:
        return True, None

    if key == "language_ietf":
        if not source_value and output_value:
            return True, (
                "來源未明確包含 LanguageIETF；MKVToolNix 根據保持不變的舊版 Language={!r} "
                "自動補出 LanguageIETF={!r}。此為語言標籤補齊，音訊／字幕內容仍直接複製。"
            ).format(source_props.get("language"), output_value)

        # Treat case and underscore/hyphen spelling as equivalent, but do not silently
        # accept a genuinely different explicit language tag.
        if source_value and output_value:
            source_normalized = str(source_value).strip().replace("_", "-").lower()
            output_normalized = str(output_value).strip().replace("_", "-").lower()
            if source_normalized == output_normalized:
                return True, None

    return False, None


# Track fields that must survive a remux unchanged. The IETF language field is
# handled by compare_track_property so MKVToolNix may backfill it only when the
# source had no explicit LanguageIETF value and the legacy language is intact.
_MKV_TRACK_COMMON_FIELDS = (
    "language", "language_ietf", "track_name", "default_track",
    "forced_track", "enabled_track", "codec_id",
)
_MKV_TRACK_FIELDS = {
    "audio": _MKV_TRACK_COMMON_FIELDS + (
        "audio_channels", "audio_sampling_frequency", "audio_bits_per_sample",
    ),
    "video": _MKV_TRACK_COMMON_FIELDS + (
        "pixel_dimensions", "display_dimensions", "display_unit",
        "default_duration", "stereo_mode", "color_matrix_coefficients",
        "color_primaries", "color_transfer_characteristics", "color_range",
        "chroma_subsampling", "bits_per_channel",
    ),
    "subtitles": _MKV_TRACK_COMMON_FIELDS,
}
_FFPROBE_TRACK_FIELDS = {
    "audio": ("codec_name", "profile", "sample_rate", "channels",
              "channel_layout", "bits_per_sample", "bits_per_raw_sample"),
    "video": ("codec_name", "profile", "width", "height", "sample_aspect_ratio",
              "display_aspect_ratio", "pix_fmt", "field_order", "color_range", "color_space", "color_transfer",
              "color_primaries", "bits_per_raw_sample", "level"),
    "subtitle": ("codec_name", "profile"),
}


def _mkv_tracks(info, kind):
    return [t for t in (info or {}).get("tracks", []) if t.get("type") == kind]


def needs_implicit_default_duration_check(source_info, output_info):
    """True only for a missing source DefaultDuration materialized on one output video track."""
    source_videos = _mkv_tracks(source_info, "video")
    output_videos = _mkv_tracks(output_info, "video")
    if len(source_videos) != 1 or len(output_videos) != 1:
        return False
    source_props = source_videos[0].get("properties", {}) or {}
    output_props = output_videos[0].get("properties", {}) or {}
    value = output_props.get("default_duration")
    return (source_props.get("default_duration") is None and
            isinstance(value, int) and not isinstance(value, bool) and value > 0)


def ffprobe_video_packet_timing(ffprobe, media_path):
    """Read video packet timestamps, durations and keyframe flags for remux validation."""
    cmd = [ffprobe, "-v", "error", "-select_streams", "v:0", "-show_streams", "-show_packets",
           "-show_entries", "stream=codec_name,avg_frame_rate,r_frame_rate,time_base:packet=pts,duration,flags",
           "-of", "json", str(media_path)]
    proc = run_text(cmd)
    if proc.returncode != 0:
        raise RuntimeError("FFprobe 影片影格時序讀取失敗：{}".format(
            (proc.stderr or proc.stdout)[-2000:]))
    try:
        return json.loads(proc.stdout)
    except Exception as exc:
        raise RuntimeError("無法解析 FFprobe 影片影格時序：{}".format(exc))


def parse_framemd5_output(output_text):
    """Parse FFmpeg framemd5 stdout for a single mapped video stream."""
    time_base = None
    frames = []
    for line in (output_text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("#tb 0:"):
            try:
                time_base = Fraction(line.split(":", 1)[1].strip())
            except (ValueError, ZeroDivisionError):
                raise ValueError("framemd5 回報無效的影片 time base")
            continue
        if line.startswith("#"):
            continue
        fields = [field.strip() for field in line.split(",")]
        if len(fields) < 6:
            raise ValueError("framemd5 輸出格式不完整")
        try:
            stream_index = int(fields[0])
            pts = int(fields[2])
            duration = int(fields[3])
        except (TypeError, ValueError):
            raise ValueError("framemd5 影格缺少有效 PTS／duration")
        digest = fields[5].lower()
        if stream_index != 0 or duration <= 0 or not re.fullmatch(r"[0-9a-f]{32,128}", digest):
            raise ValueError("framemd5 影格資料不符合單一影片軌驗證要求")
        frames.append((pts, duration, digest))

    if time_base is None or time_base <= 0 or not frames:
        raise ValueError("framemd5 沒有提供有效 time base 或可解碼影格")
    return {"time_base": time_base, "frames": frames}


def ffmpeg_video_framemd5(ffmpeg, media_path):
    """Decode one video stream and return decoded-frame MD5s and their time base.

    Output is kept in memory only; no frame files or intermediate media are written.
    """
    cmd = [ffmpeg, "-hide_banner", "-nostdin", "-v", "error", "-i", str(media_path),
           "-map", "0:v:0", "-an", "-sn", "-dn", "-f", "framemd5", "-"]
    proc = run_text(cmd)
    if proc.returncode != 0:
        raise RuntimeError("FFmpeg 逐影格解碼雜湊失敗（{}）：{}".format(
            media_path, (proc.stderr or proc.stdout)[-2500:]))
    try:
        return parse_framemd5_output(proc.stdout)
    except ValueError as exc:
        raise RuntimeError(str(exc))


def compare_framemd5_summaries(source, output, context="最終 MKV ", skip_source_prefix=0):
    """Compare decoded pixels/timing, optionally excluding a verified undecodable packet prefix.

    FFmpeg may emit concealment frames while decoding H.264/HEVC packets that occur
    before the first random-access keyframe. MKVToolNix can discard those packets.
    Only the caller that has independently verified that exact packet prefix may
    ask us to skip the same number of leading source framemd5 records. The remaining
    source sequence must match every output frame in count, pixels and timing.
    """
    src_frames = source["frames"]
    out_frames = output["frames"]
    if not isinstance(skip_source_prefix, int) or isinstance(skip_source_prefix, bool) or skip_source_prefix < 0:
        return False, "{}逐影格解碼比較失敗：來源前導影格略過數無效。".format(context)
    if skip_source_prefix >= len(src_frames):
        return False, "{}逐影格解碼比較失敗：要略過的來源前導影格數超出來源影格數。".format(context)
    if skip_source_prefix:
        src_frames = src_frames[skip_source_prefix:]
    if len(src_frames) != len(out_frames):
        return False, "{}逐影格解碼比較失敗：略過來源前導影格後，來源／輸出可解碼影格數仍不同（{}／{}）。".format(
            context, len(src_frames), len(out_frames))
    if not src_frames:
        return False, "{}逐影格解碼比較失敗：沒有可比較的影格。".format(context)

    # FFmpeg's framemd5 source time base can equal one CFR frame period
    # (e.g. 1001/24000), not a sub-frame clock resolution. The MKV output
    # time base is the quantization clock we must bound to <= 1 ms.
    if source["time_base"] == output["time_base"]:
        # FFmpeg may choose one exact CFR-frame tick (e.g. 1001/24000) as
        # framemd5's time base. When both decodes use that same clock, require
        # exact relative PTS and duration equality rather than treating one
        # whole frame as an allowed timestamp error.
        tolerance = Fraction(0, 1)
    else:
        tolerance = max(source["time_base"], output["time_base"])
        if tolerance > Fraction(1, 1000):
            return False, "{}逐影格解碼比較失敗：來源／輸出 framemd5 時間基準不同，且較粗時間刻度超過 1 ms。".format(context)

    src_origin = Fraction(src_frames[0][0]) * source["time_base"]
    out_origin = Fraction(out_frames[0][0]) * output["time_base"]
    for index, (src_frame, out_frame) in enumerate(zip(src_frames, out_frames), 1):
        if src_frame[2] != out_frame[2]:
            return False, "{}逐影格解碼比較失敗：第 {} 個解碼影格 MD5 不一致。".format(context, index)
        src_pts = Fraction(src_frame[0]) * source["time_base"]
        out_pts = Fraction(out_frame[0]) * output["time_base"]
        src_duration = Fraction(src_frame[1]) * source["time_base"]
        out_duration = Fraction(out_frame[1]) * output["time_base"]
        if abs((src_pts - src_origin) - (out_pts - out_origin)) > tolerance:
            return False, "{}逐影格解碼比較失敗：第 {} 個影格相對 PTS 改變，超過 1 ms。".format(
                context, index)
        if abs(src_duration - out_duration) > tolerance:
            return False, "{}逐影格解碼比較失敗：第 {} 個影格 duration 改變，超過 1 ms。".format(
                context, index)

    prefix_note = "略過來源前導解碼影格 {} 幀後，".format(skip_source_prefix) if skip_source_prefix else ""
    return True, ("{}{}來源／輸出解碼影格完全一致：{} 幀的 MD5 逐幀相同，"
                  "相對 PTS／duration 均在 {} ns 量化誤差內一致。".format(
                      context, prefix_note, len(src_frames), int(tolerance * 1_000_000_000)))


def compare_decoded_video_frame_sequences(ffmpeg, source_path, output_path, context="最終 MKV ",
                                          skip_source_prefix=0):
    """Require the retained decoded source suffix and output frames to be pixel-identical."""
    try:
        source = ffmpeg_video_framemd5(ffmpeg, source_path)
        output = ffmpeg_video_framemd5(ffmpeg, output_path)
    except Exception as exc:
        return False, "{}無法完成來源／輸出逐影格解碼比較：{}。".format(context, exc)
    return compare_framemd5_summaries(
        source, output, context=context, skip_source_prefix=skip_source_prefix)


def leading_keyframe_packet_drop_candidate(source_timing, output_timing):
    """Return the exact removable leading packet-prefix count, or a rejection reason."""
    source_streams = (source_timing or {}).get("streams", [])
    output_streams = (output_timing or {}).get("streams", [])
    source_packets = (source_timing or {}).get("packets", [])
    output_packets = (output_timing or {}).get("packets", [])
    if len(source_streams) != 1 or len(output_streams) != 1:
        return None, "來源／輸出不是各一條影片串流。"
    if len(source_packets) == len(output_packets):
        return 0, ""
    missing = len(source_packets) - len(output_packets)
    if missing <= 0:
        return None, "輸出影片封包數多於來源或封包差額方向不符。"
    source_codec = str(source_streams[0].get("codec_name") or "").lower()
    output_codec = str(output_streams[0].get("codec_name") or "").lower()
    if source_codec != output_codec or source_codec not in {"h264", "hevc"}:
        return None, "只有相同的 H.264／HEVC 軌道可套用前導封包規則。"
    source_flags = [str(packet.get("flags") or "") for packet in source_packets]
    output_flags = [str(packet.get("flags") or "") for packet in output_packets]
    first_keyframe = next((i for i, flags in enumerate(source_flags) if "K" in flags), None)
    if first_keyframe != missing or not output_flags or "K" not in output_flags[0]:
        return None, "封包差額不是第一個關鍵影格之前的完整前綴，或輸出未從關鍵影格開始。"
    return missing, ""


def compare_implicit_default_duration_timing(source_timing, output_timing, output_default_duration,
                                             context="最終 MKV ", decoded_frames_identical=False):
    """Verify CFR timing before accepting DefaultDuration materialized by Matroska.

    MKVToolNix may intentionally discard H.264/HEVC packets before the first keyframe.
    A packet-count reduction is accepted only when it is exactly that leading prefix,
    the output starts at that first keyframe, and an independent FFmpeg framemd5 decode
    proves the complete displayed frame sequence is unchanged.
    """
    def one_stream(data, side):
        streams = (data or {}).get("streams", [])
        if len(streams) != 1:
            raise ValueError("{}影片串流數量不是 1".format(side))
        stream = streams[0]
        try:
            avg = Fraction(str(stream.get("avg_frame_rate")))
            real = Fraction(str(stream.get("r_frame_rate")))
            time_base = Fraction(str(stream.get("time_base")))
        except (ValueError, ZeroDivisionError, TypeError):
            raise ValueError("{}影片缺少有效的 avg_frame_rate／r_frame_rate／time_base".format(side))
        if avg <= 0 or real <= 0 or time_base <= 0:
            raise ValueError("{}影片影格率或 time_base 無效".format(side))
        packets = (data or {}).get("packets", [])
        if len(packets) < 2:
            raise ValueError("{}影片封包不足，無法驗證影格時間".format(side))
        pts_values, duration_values, flags = [], [], []
        for index, packet in enumerate(packets, 1):
            try:
                pts = int(packet["pts"])
                duration = int(packet["duration"])
            except (KeyError, TypeError, ValueError):
                raise ValueError("{}影片第 {} 個封包缺少 PTS 或 duration".format(side, index))
            if duration <= 0:
                raise ValueError("{}影片第 {} 個封包 duration 無效".format(side, index))
            pts_values.append(Fraction(pts) * time_base)
            duration_values.append(Fraction(duration) * time_base)
            flags.append(str(packet.get("flags") or ""))
        return (avg, real, time_base, pts_values, duration_values,
                flags, str(stream.get("codec_name") or "").lower())

    try:
        (src_avg, src_real, src_tb, src_pts, src_durations, src_flags, src_codec) = one_stream(source_timing, "來源")
        (out_avg, out_real, out_tb, out_pts, out_durations, out_flags, out_codec) = one_stream(output_timing, "輸出")
    except ValueError as exc:
        return False, "{}DefaultDuration 正規化拒絕：{}。".format(context, exc), 0

    if not (src_avg == src_real == out_avg == out_real):
        return False, "{}DefaultDuration 正規化拒絕：來源／輸出不是完全一致的固定影格率（avg_frame_rate／r_frame_rate）。".format(context), 0

    expected_duration = Fraction(1, 1) / src_avg
    expected_ns = int(round(expected_duration * 1_000_000_000))
    if abs(int(output_default_duration) - expected_ns) > 1:
        return False, ("{}DefaultDuration 正規化拒絕：輸出 DefaultDuration={} ns，"
                       "但 FFprobe 固定影格率 {} 對應約 {} ns。".format(
                           context, output_default_duration, str(src_avg), expected_ns)), 0

    prefix_drop_count = 0
    if len(src_pts) != len(out_pts):
        missing = len(src_pts) - len(out_pts)
        if missing <= 0:
            return False, "{}DefaultDuration 正規化拒絕：輸出影片封包數多於來源（{}／{}）。".format(
                context, len(src_pts), len(out_pts)), 0
        first_keyframe = next((i for i, value in enumerate(src_flags) if "K" in value), None)
        codec_supported = src_codec == out_codec and src_codec in {"h264", "hevc"}
        if first_keyframe != missing or not out_flags or "K" not in out_flags[0]:
            return False, ("{}DefaultDuration 正規化拒絕：來源／輸出影片封包數不同（{}／{}），"
                           "差額不是第一個關鍵影格之前的完整前綴。".format(
                               context, len(src_pts), len(out_pts))), 0
        if not codec_supported:
            return False, ("{}DefaultDuration 正規化拒絕：只有 H.264／HEVC 第一個關鍵影格前的封包"
                           "可套用 MKVToolNix 前導封包規則（目前 codec：{}）。".format(
                               context, src_codec or "未知")), 0
        if not decoded_frames_identical:
            return False, ("{}DefaultDuration 正規化拒絕：偵測到第一個關鍵影格前有 {} 個來源封包被省略，"
                           "但尚未證明解碼影格完全一致。".format(context, missing)), 0
        prefix_drop_count = missing
        src_pts = src_pts[missing:]
        src_durations = src_durations[missing:]
        src_flags = src_flags[missing:]

    tolerance = max(src_tb, out_tb)
    if tolerance > Fraction(1, 1000):
        return False, "{}DefaultDuration 正規化拒絕：來源／輸出時間戳精度不足以確認 1 ms 內的影格時序。".format(context), 0

    for index, (src_duration, out_duration) in enumerate(zip(src_durations, out_durations), 1):
        if abs(src_duration - expected_duration) > tolerance:
            return False, "{}DefaultDuration 正規化拒絕：來源第 {} 個封包不是固定影格時長。".format(context, index), 0
        if abs(out_duration - expected_duration) > tolerance:
            return False, "{}DefaultDuration 正規化拒絕：輸出第 {} 個封包時長與固定影格率不符。".format(context, index), 0
        if abs(src_duration - out_duration) > tolerance:
            return False, "{}DefaultDuration 正規化拒絕：第 {} 個封包時長前後不同。".format(context, index), 0

    src_origin, out_origin = src_pts[0], out_pts[0]
    for index, (src_pts_value, out_pts_value) in enumerate(zip(src_pts, out_pts), 1):
        if abs((src_pts_value - src_origin) - (out_pts_value - out_origin)) > tolerance:
            return False, "{}DefaultDuration 正規化拒絕：第 {} 個封包的相對 PTS 改變，超過時間戳精度容許值。".format(
                context, index), 0

    if prefix_drop_count:
        message = ("{}來源未提供 Matroska DefaultDuration；輸出補入 {} ns。"
                   "來源開頭 {} 個 H.264／HEVC 非關鍵封包位於第一個 keyframe 之前，符合 MKVToolNix 的前導封包處理規則；"
                   "已另以 FFmpeg framemd5 證明來源／輸出 {} 個可解碼影格逐幀相同，"
                   "其封包 duration 與相對 PTS 亦在 {} ns 量化誤差內一致。".format(
                       context, output_default_duration, prefix_drop_count,
                       len(out_pts), int(tolerance * 1_000_000_000)))
    else:
        message = ("{}來源未提供 Matroska DefaultDuration；輸出補入 {} ns。"
                   "FFprobe 確認來源／輸出均為 CFR {}，且逐封包 duration 與相對 PTS 在 {} ns 量化誤差內一致。".format(
                       context, output_default_duration, str(src_avg), int(tolerance * 1_000_000_000)))
    return True, message, prefix_drop_count


def _mkv_codec_id_matches_display(codec_id, codec_description):
    """Return True only for known Matroska video CodecID/display-name pairs."""
    codec_id = str(codec_id or "").strip().upper()
    description = str(codec_description or "").strip().lower()
    known = {
        "V_MPEGH/ISO/HEVC": ("hevc", "h.265", "h265", "mpeg-h"),
        "V_MPEG4/ISO/AVC": ("avc", "h.264", "h264"),
        "V_AV1": ("av1",),
        "V_VP9": ("vp9",),
        "V_MPEG2": ("mpeg-2", "mpeg2"),
        "V_THEORA": ("theora",),
    }
    markers = known.get(codec_id)
    return bool(markers and description and any(marker in description for marker in markers))


def _mkv_audio_codec_id_matches_display(codec_id, codec_description):
    """Return True only for the AAC Matroska CodecID and an AAC codec description."""
    codec_id = str(codec_id or "").strip().upper()
    description = str(codec_description or "").strip().lower()
    # Deliberately limited to this observed MP4 -> MKV case. Audio codec IDs other
    # than A_AAC are not normalized by this helper.
    return codec_id == "A_AAC" and bool(re.search(r"\baac\b", description))


def has_implicit_single_video_default_promotion(source_info, output_info):
    """True only when one unflagged source video track becomes explicit Default Yes."""
    source_videos = _mkv_tracks(source_info, "video")
    output_videos = _mkv_tracks(output_info, "video")
    if len(source_videos) != 1 or len(output_videos) != 1:
        return False
    source_props = source_videos[0].get("properties", {}) or {}
    output_props = output_videos[0].get("properties", {}) or {}
    return source_props.get("default_track") is None and output_props.get("default_track") is True


def compare_mkv_track_metadata(source_info, output_info, kinds=("video", "audio"), context="輸出",
                                ignored_fields=None, allow_implicit_default_duration=False):
    """Strictly compare protected MKV track metadata in source/output order."""
    notes = []
    for kind in kinds:
        source_tracks = _mkv_tracks(source_info, kind)
        output_tracks = _mkv_tracks(output_info, kind)
        label = {"video": "影片", "audio": "音訊", "subtitles": "字幕"}.get(kind, kind)
        if len(source_tracks) != len(output_tracks):
            return False, "{}{}軌數量不同（來源 {}、輸出 {}）。".format(
                context, label, len(source_tracks), len(output_tracks)), notes
        ignored = set((ignored_fields or {}).get(kind, ()))
        fields = tuple(x for x in _MKV_TRACK_FIELDS.get(kind, _MKV_TRACK_COMMON_FIELDS) if x not in ignored)
        for idx, (src_track, out_track) in enumerate(zip(source_tracks, output_tracks), 1):
            if src_track.get("codec") != out_track.get("codec"):
                return False, "{}第 {} 條{}的 codec 描述改變（來源={!r}；輸出={!r}）。".format(
                    context, idx, label, src_track.get("codec"), out_track.get("codec")), notes
            sp = src_track.get("properties", {}) or {}
            op = out_track.get("properties", {}) or {}
            implicit_default_display_size = (
                kind == "video" and len(source_tracks) == 1 and len(output_tracks) == 1 and
                sp.get("display_dimensions") is None and op.get("display_dimensions") and
                sp.get("pixel_dimensions") and
                sp.get("pixel_dimensions") == op.get("pixel_dimensions") and
                op.get("display_dimensions") == op.get("pixel_dimensions")
            )
            for key in fields:
                # An MP4 source may expose coded pixel dimensions but no Matroska
                # display_dimensions element. MKVToolNix then writes the Matroska
                # default display size equal to the coded pixel size. Accept only
                # that no-scaling representation for a single-video source: source
                # and output coded dimensions must match, and output display size
                # must equal those coded dimensions. FFprobe additionally compares
                # sample_aspect_ratio and display_aspect_ratio below. Any explicit
                # display-size change, anamorphic display size, missing coded size,
                # or multi-video case remains a hard failure.
                if key == "display_dimensions" and implicit_default_display_size:
                    notes.append(
                        "{}來源只有一條影片軌且未提供 Matroska display_dimensions；"
                        "輸出僅補入與來源／輸出 coded pixel_dimensions 完全相同的預設顯示尺寸 {}。"
                        "FFprobe 仍嚴格比較 sample_aspect_ratio、display_aspect_ratio 及解析度。".format(
                            context, op.get(key))
                    )
                    continue
                # Matroska's DisplayUnit default is 0 (pixels). If the source
                # container had no display size/unit at all and the output only
                # materializes the exact coded pixel size checked above, None -> 0
                # is the corresponding default-unit representation, not a scaling
                # or aspect-ratio instruction. Other units remain strictly checked.
                if (kind == "video" and key == "display_unit" and
                        implicit_default_display_size and sp.get(key) is None and
                        op.get(key) == 0):
                    notes.append(
                        "{}來源未提供影片 DisplayUnit；輸出只為預設像素顯示尺寸明確寫入 DisplayUnit=0。"
                        "此例外僅依附於已確認未改變的 coded／display 尺寸。".format(context)
                    )
                    continue
                # A non-Matroska source may omit Matroska DefaultDuration while
                # MKVToolNix materializes it in the output. This field is accepted
                # only when verify_output has independently checked CFR and every
                # video packet's duration / relative PTS with FFprobe.
                if (kind == "video" and key == "default_duration" and
                        allow_implicit_default_duration and sp.get(key) is None and
                        isinstance(op.get(key), int) and not isinstance(op.get(key), bool) and
                        op.get(key) > 0):
                    notes.append(
                        "{}第 {} 條影片來源未提供 Matroska DefaultDuration；輸出補入 {} ns。"
                        "已由 FFprobe 固定影格率及逐封包時序比對確認。".format(
                            context, idx, op.get(key))
                    )
                    continue
                # MP4 and other non-Matroska containers do not necessarily expose a
                # Matroska CodecID in mkvmerge -J for the source. During remux,
                # MKVToolNix assigns the corresponding Matroska CodecID to the output.
                # Accept only a missing source CodecID -> known output video CodecID
                # when mkvmerge's human-readable codec description is unchanged and
                # explicitly matches that CodecID. FFprobe independently checks the
                # elementary codec/profile/pixel format/dimensions/color fields later.
                if (kind == "video" and key == "codec_id" and
                        sp.get(key) is None and op.get(key) and
                        src_track.get("codec") and
                        src_track.get("codec") == out_track.get("codec") and
                        _mkv_codec_id_matches_display(op.get(key), out_track.get("codec"))):
                    notes.append(
                        "{}第 {} 條影片來源未提供 Matroska CodecID；重新封裝後 MKVToolNix 指派 {}。"
                        "來源／輸出的 codec 描述均為 {!r}，且 CodecID 與描述相符；"
                        "後續 FFprobe 仍會嚴格核對實際 codec_name、Profile、像素格式及解析度。".format(
                            context, idx, op.get(key), out_track.get("codec"))
                    )
                    continue
                # MP4 readers can report the QuickTime audio sample-size field as
                # audio_bits_per_sample=16 even for compressed AAC. Matroska's Audio
                # BitDepth is optional and, per RFC 9559, is mostly used for PCM;
                # MKVToolNix may therefore omit it for an A_AAC track. Accept only
                # the exact single-AAC MP4 -> MKV representation seen in the user's
                # run: source CodecID absent, output A_AAC, identical AAC description,
                # source sample-size 16 and output BitDepth absent. FFprobe still
                # strictly compares codec/profile/sample rate/channels/layout and its
                # bit-depth fields; audio packet counts and full decode remain strict.
                if (kind == "audio" and key == "audio_bits_per_sample" and
                        len(source_tracks) == 1 and len(output_tracks) == 1 and
                        sp.get(key) == 16 and op.get(key) is None and
                        sp.get("codec_id") is None and op.get("codec_id") == "A_AAC" and
                        src_track.get("codec") and
                        src_track.get("codec") == out_track.get("codec") and
                        _mkv_audio_codec_id_matches_display(op.get("codec_id"), out_track.get("codec"))):
                    notes.append(
                        "{}第 {} 條 AAC 音訊來源 MP4 回報 sample-size=16 bits，但輸出 Matroska 未寫入可選的 BitDepth。"
                        "Matroska BitDepth 主要用於 PCM；此單 AAC 軌僅正規化該容器欄位。"
                        "FFprobe 的 codec/profile/取樣率/聲道配置/位深欄位、音訊封包數及完整解碼仍嚴格驗證。".format(
                            context, idx)
                    )
                    continue
                # MP4 readers may not expose a Matroska CodecID for AAC audio,
                # while the MKV output materializes A_AAC. Accept only the exact
                # observed single-audio-track None -> A_AAC case and only when the
                # human-readable codec description is unchanged and explicitly
                # identifies AAC. FFprobe still strictly compares codec_name,
                # profile, sample rate, channels/layout and bit depth; packet-count
                # equality and the full audio/video decode remain mandatory.
                if (kind == "audio" and key == "codec_id" and
                        len(source_tracks) == 1 and len(output_tracks) == 1 and
                        sp.get(key) is None and op.get(key) == "A_AAC" and
                        src_track.get("codec") and
                        src_track.get("codec") == out_track.get("codec") and
                        _mkv_audio_codec_id_matches_display(op.get(key), out_track.get("codec"))):
                    notes.append(
                        "{}第 {} 條音訊來源未提供 Matroska CodecID；重新封裝後 MKVToolNix 指派 A_AAC。"
                        "來源／輸出的 codec 描述均為 {!r} 且明確標示 AAC；"
                        "FFprobe 仍嚴格核對 codec_name、Profile、取樣率、聲道配置與位深，"
                        "音訊封包數及完整解碼仍須通過。".format(
                            context, idx, out_track.get("codec"))
                    )
                    continue
                # MP4 sources sometimes do not expose a Matroska default_track flag
                # (mkvmerge -J reports None). When there is exactly one video track,
                # that sole track is the only possible video choice; MKVToolNix may
                # write the equivalent state explicitly as default_track=True. Accept
                # only this narrow None -> True normalization. Do not relax multi-video
                # files, audio/subtitle flags, explicit False, or output False.
                if (kind == "video" and key == "default_track" and
                        len(source_tracks) == 1 and sp.get(key) is None and
                        op.get(key) is True):
                    notes.append(
                        "{}來源只有一條影片軌且未明確提供 Default 旗標；輸出將唯一影片軌明確標記為 Default Yes。"
                        "兩者在單影片軌情況下等效，其他影片軌屬性仍嚴格比對。".format(context)
                    )
                    continue
                # MP4 sources commonly omit Matroska's Forced flag on video tracks.
                # MKVToolNix writes the default value (False) explicitly during remux.
                # This is an equivalent representation only for a single video track;
                # do not relax this for audio/subtitle tracks or multi-video sources.
                if (kind == "video" and key == "forced_track" and
                        len(source_tracks) == 1 and sp.get(key) is None and
                        op.get(key) is False):
                    notes.append(
                        "{}來源只有一條影片軌且未明確提供 Forced 旗標；輸出明確標記 Forced No。"
                        "來源未設定與明確非強制在此單影片軌情況下等效。音訊／字幕 Forced 旗標仍嚴格比對。".format(context)
                    )
                    continue
                # MP4 sources commonly have no Matroska Forced flag for audio.
                # When there is exactly one audio track, MKVToolNix materializes
                # Matroska's default FlagForced value (False) in the output. With
                # no alternate audio track, None -> False does not change track
                # selection. Keep this deliberately narrower than a general audio
                # relaxation: multi-audio sources, explicit source values, output
                # Forced Yes, and all subtitle Forced flags remain strict. Every
                # other audio property (language, Default, codec, channels, rate,
                # bit depth and packet count) is still independently verified.
                if (kind == "audio" and key == "forced_track" and
                        len(source_tracks) == 1 and len(output_tracks) == 1 and
                        sp.get(key) is None and op.get(key) is False):
                    notes.append(
                        "{}來源只有一條音訊軌且未提供 Matroska Forced 旗標；輸出明確標記 Forced No。"
                        "僅接受單音訊軌的 None -> False 預設值正規化；音訊語言、Default、編碼、聲道、"
                        "取樣率、位深及封包數仍嚴格驗證，多音訊軌與字幕 Forced 旗標不放寬。".format(context)
                    )
                    continue
                matches, note = compare_track_property(sp, op, key)
                if not matches:
                    return False, "{}第 {} 條{}的 {} 改變（來源={!r}；輸出={!r}）。".format(
                        context, idx, label, key, sp.get(key), op.get(key)), notes
                if note:
                    notes.append("{}第 {} 條{}：{}".format(context, idx, label, note))
    return True, "{}軌道數量與受保護的 Matroska 軌道屬性一致。".format(context), notes


def compare_ffprobe_stream_metadata(source_probe, output_probe, kinds=("video", "audio"), context="輸出",
                                    check_disposition=True, allow_implicit_single_video_default=False):
    """Compare decoded stream identity/format metadata, ignoring container indexes/timestamps."""
    notes = []
    source_streams = (source_probe or {}).get("streams", [])
    output_streams = (output_probe or {}).get("streams", [])
    for kind in kinds:
        src = [x for x in source_streams if x.get("codec_type") == kind]
        out = [x for x in output_streams if x.get("codec_type") == kind]
        label = {"video": "影片", "audio": "音訊", "subtitle": "字幕"}.get(kind, kind)
        if len(src) != len(out):
            return False, "{}FFprobe 的{}串流數量不同（來源 {}、輸出 {}）。".format(
                context, label, len(src), len(out)), notes
        for idx, (a, b) in enumerate(zip(src, out), 1):
            for key in _FFPROBE_TRACK_FIELDS.get(kind, ("codec_name",)):
                av, bv = a.get(key), b.get(key)
                if av != bv:
                    return False, "{}第 {} 條{}的 FFprobe {} 改變（來源={!r}；輸出={!r}）。".format(
                        context, idx, label, key, av, bv), notes
            # Matroska track flags are independently checked via mkvmerge -J.
            if check_disposition:
                ad, bd = a.get("disposition", {}) or {}, b.get("disposition", {}) or {}
                for key in ("default", "forced"):
                    if ad.get(key) != bd.get(key):
                        if (kind == "video" and key == "default" and
                                allow_implicit_single_video_default and len(src) == 1 and len(out) == 1):
                            notes.append(
                                "{}來源只有一條影片軌且來源容器未明確提供 Default；輸出將唯一影片軌標記為 Default。"
                                "FFprobe disposition 的差異視為單軌預設資訊正規化。".format(context)
                            )
                            continue
                        return False, "{}第 {} 條{}的 FFprobe disposition.{} 改變。".format(
                            context, idx, label, key), notes
    return True, "{}FFprobe 串流格式屬性一致。".format(context), notes


def ffprobe_packet_summary(ffprobe, media_path):
    """Return per-type, in-order codec/packet-count summaries from FFprobe."""
    cmd = [ffprobe, "-v", "error", "-count_packets",
           "-show_entries", "stream=index,codec_type,codec_name,nb_read_packets",
           "-of", "json", str(media_path)]
    proc = run_text(cmd)
    if proc.returncode != 0:
        raise RuntimeError("FFprobe 封包計數失敗（{}）：{}".format(
            media_path, (proc.stderr or proc.stdout)[-2000:]))
    data = json.loads(proc.stdout)
    result = {}
    for kind in ("video", "audio", "subtitle"):
        entries = []
        for stream in data.get("streams", []):
            if stream.get("codec_type") != kind:
                continue
            raw_count = stream.get("nb_read_packets")
            try:
                packet_count = int(raw_count)
            except (TypeError, ValueError):
                packet_count = None
            entries.append((stream.get("codec_name"), packet_count, stream.get("index")))
        result[kind] = entries
    return result


def compare_ffprobe_packet_counts(ffprobe, source_path, output_path, kinds=("audio", "subtitle"), context="輸出",
                                  logfile=None, source_audio_stream_indices=None):
    """Detect missing/dropped packets for copied audio/subtitle streams."""
    try:
        source_summary = ffprobe_packet_summary(ffprobe, source_path)
        output_summary = ffprobe_packet_summary(ffprobe, output_path)
    except Exception as exc:
        return False, "{}無法比較來源與輸出封包數：{}".format(context, exc)
    notes = []
    for kind in kinds:
        src = source_summary.get(kind, [])
        out = output_summary.get(kind, [])
        if kind == "audio" and source_audio_stream_indices is not None:
            selected = {int(x) for x in source_audio_stream_indices}
            src = [entry for entry in src if entry[2] is not None and int(entry[2]) in selected]
        label = {"audio": "音訊", "subtitle": "字幕", "video": "影片"}.get(kind, kind)
        if len(src) != len(out):
            return False, "{}{}封包摘要的軌道數量不同（來源 {}、輸出 {}）。".format(context, label, len(src), len(out))
        for idx, (a, b) in enumerate(zip(src, out), 1):
            if a[0] != b[0]:
                return False, "{}第 {} 條{}封包 codec 不一致。".format(context, idx, label)
            if a[1] is not None and b[1] is not None:
                if a[1] != b[1]:
                    return False, "{}第 {} 條{}封包數不同（來源 {}、輸出 {}）。".format(
                        context, idx, label, a[1], b[1])
            else:
                notes.append("{}第 {} 條{}無法從 FFprobe 取得雙方封包數；已由完整解碼／字幕可讀性檢查補充驗證。".format(
                    context, idx, label))
    for note in notes:
        write_log(logfile, note)
    return True, "{}音訊／字幕封包數比對完成。".format(context)


def _chapter_signature(info):
    # mkvmerge -J emits a structured chapter tree. Preserve ordering and all
    # chapter timing, UID, edition flags, display strings and language metadata.
    chapters = (info or {}).get("chapters", []) or []
    return json.dumps(chapters, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _attachment_signature(info):
    # Attachment IDs may be renumbered by a remux; content identity fields may not.
    fields = ("file_name", "content_type", "description", "size")
    attachments = (info or {}).get("attachments", []) or []
    return [tuple((key, item.get(key)) for key in fields) for item in attachments]


def compare_chapters_and_attachments(source_info, output_info, context="輸出"):
    if _chapter_signature(source_info) != _chapter_signature(output_info):
        return False, "{}章節結構、章節時間、UID 或章節顯示資訊與來源不一致。".format(context)
    source_attachments = _attachment_signature(source_info)
    output_attachments = _attachment_signature(output_info)
    if source_attachments != output_attachments:
        return False, "{}附件的數量、檔名、MIME 類型、描述或大小與來源不一致。".format(context)
    return True, "{}章節結構及附件清單 metadata 與來源一致（附件 ID 可因重新封裝重新編號）。".format(context)


def _sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def compare_mkv_attachment_payloads(mkvextract, source_path, source_info, output_path, output_info, logfile):
    """Hash-check attachments when the original container is Matroska."""
    source_attachments = (source_info or {}).get("attachments", []) or []
    output_attachments = (output_info or {}).get("attachments", []) or []
    if not source_attachments:
        return True, "來源沒有附件，無需執行附件內容雜湊比對。"
    if Path(source_path).suffix.lower() != ".mkv":
        write_log(logfile, "附件內容雜湊：來源不是 MKV（{}），mkvextract 無法直接讀取原始容器；已比對附件數量、檔名、MIME、描述及大小。".format(
            Path(source_path).suffix.lower()))
        return True, "附件 metadata／大小一致；非 MKV 來源未執行原始附件 SHA-256 比對。"
    if not mkvextract:
        return False, "來源含附件但未找到 mkvextract，無法完成附件內容 SHA-256 驗證。"
    if len(source_attachments) != len(output_attachments):
        return False, "附件數量不一致，無法執行內容雜湊比對。"
    try:
        with tempfile.TemporaryDirectory(prefix=".frame-attachment-check-", dir=str(Path(output_path).parent)) as temp_dir:
            temp_root = Path(temp_dir)
            extracted = {"source": [], "output": []}
            for label, media_path, items in (("source", source_path, source_attachments),
                                             ("output", output_path, output_attachments)):
                for index, item in enumerate(items, 1):
                    attachment_id = item.get("id")
                    if attachment_id is None:
                        return False, "{}第 {} 個附件缺少 MKVToolNix Attachment ID。".format(label, index)
                    destination = temp_root / "{}-attachment-{}.bin".format(label, index)
                    proc = run_text([mkvextract, "attachments", str(media_path),
                                     "{}:{}".format(attachment_id, destination)])
                    diagnostic = "\n".join(x for x in (proc.stdout, proc.stderr) if x).strip()
                    if diagnostic:
                        write_block(logfile, "{}附件 {} 提取輸出".format(label, attachment_id), diagnostic)
                    if proc.returncode not in (0, 1) or not destination.is_file():
                        return False, "{}附件 ID {} 無法提取，無法確認附件內容完整性。".format(label, attachment_id)
                    extracted[label].append(destination)
            for index, (src_path, out_path) in enumerate(zip(extracted["source"], extracted["output"]), 1):
                if src_path.stat().st_size != out_path.stat().st_size:
                    return False, "第 {} 個附件提取後大小不同。".format(index)
                src_hash, out_hash = _sha256_file(src_path), _sha256_file(out_path)
                if src_hash != out_hash:
                    return False, "第 {} 個附件 SHA-256 不一致，附件內容可能已改變。".format(index)
                write_log(logfile, "附件 {} SHA-256 驗證通過：{}".format(index, src_hash))
        return True, "{} 個附件的 SHA-256 內容比對全部通過。".format(len(source_attachments))
    except Exception as exc:
        return False, "附件內容驗證發生錯誤：{}".format(exc)


def fsync_directory(path):
    """Best-effort directory fsync after publishing a completed output."""
    try:
        fd = os.open(str(path), os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError:
        pass


def atomic_publish_no_overwrite(staged_path, final_path):
    """Atomically publish a verified file without ever replacing an existing path.

    Prefer macOS renamex_np(RENAME_EXCL), then an atomic hard-link (supported by
    APFS/HFS+), with an O_EXCL reservation fallback for filesystems such as exFAT.
    The staged and final paths must be on the same volume.
    """
    staged_path = Path(staged_path)
    final_path = Path(final_path)
    if not staged_path.is_file() or staged_path.stat().st_size <= 0:
        raise RuntimeError("暫存輸出不存在或為空，拒絕發布。")
    try:
        with staged_path.open("rb") as staged_stream:
            os.fsync(staged_stream.fileno())
    except OSError:
        # Some removable/network filesystems do not support fsync on a read-only handle.
        pass
    final_path.parent.mkdir(parents=True, exist_ok=True)
    if final_path.exists():
        raise FileExistsError("正式輸出已存在，拒絕覆蓋：{}".format(final_path))

    # Darwin supports exclusive rename, avoiding an overwrite race and a visible
    # empty placeholder file on external volumes that do not support hard links.
    if sys.platform == "darwin":
        try:
            libc = ctypes.CDLL(None, use_errno=True)
            renamex = getattr(libc, "renamex_np")
            renamex.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
            renamex.restype = ctypes.c_int
            result = renamex(os.fsencode(staged_path), os.fsencode(final_path), 0x00000004)  # RENAME_EXCL
            if result == 0:
                fsync_directory(final_path.parent)
                return
            err = ctypes.get_errno()
            if err == errno.EEXIST:
                raise FileExistsError("正式輸出已存在，拒絕覆蓋：{}".format(final_path))
            if err not in (errno.ENOTSUP, errno.EOPNOTSUPP, errno.ENOSYS, errno.EINVAL):
                raise OSError(err, os.strerror(err), str(final_path))
        except AttributeError:
            pass

    # Atomic link creation is no-clobber and works on APFS/HFS+ and most POSIX FS.
    try:
        os.link(str(staged_path), str(final_path))
        # The final directory entry is already atomically committed. If removing
        # the hidden staging link fails, the job is still successfully published;
        # the staging-directory cleanup will retry later.
        try:
            staged_path.unlink()
        except OSError:
            pass
        fsync_directory(final_path.parent)
        return
    except FileExistsError:
        raise FileExistsError("正式輸出已存在，拒絕覆蓋：{}".format(final_path))
    except OSError as exc:
        if exc.errno not in (errno.EPERM, errno.EOPNOTSUPP, errno.ENOTSUP, errno.EXDEV, errno.ENOSYS, errno.EACCES):
            raise

    # exFAT and some network filesystems do not implement hard links. Reserve the
    # destination exclusively, then atomically replace only our own empty marker.
    fd = os.open(str(final_path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        reserved = os.fstat(fd)
        current = os.stat(str(final_path), follow_symlinks=False)
        if (reserved.st_dev, reserved.st_ino) != (current.st_dev, current.st_ino):
            raise RuntimeError("正式輸出路徑的保留標記已被替換，拒絕發布。")
        os.replace(str(staged_path), str(final_path))
        fsync_directory(final_path.parent)
    except Exception:
        try:
            current = os.stat(str(final_path), follow_symlinks=False)
            if (current.st_dev, current.st_ino) == (reserved.st_dev, reserved.st_ino):
                final_path.unlink()
        except OSError:
            pass
        raise
    finally:
        os.close(fd)


def cleanup_empty_staging_directory(staging_dir, logfile):
    """Remove only an empty staging directory; leave leftovers for manual recovery."""
    if not staging_dir:
        return
    staging_dir = Path(staging_dir)
    if not staging_dir.exists():
        return
    try:
        leftovers = list(staging_dir.iterdir())
        if leftovers:
            write_log(logfile, "暫存目錄仍有未清理檔案，為避免遺失診斷資料而保留：{}；內容：{}".format(
                staging_dir, "、".join(x.name for x in leftovers)))
            return
        staging_dir.rmdir()
    except OSError as exc:
        write_log(logfile, "暫存目錄清理未完成，已保留供檢查：{}；{}".format(staging_dir, exc))


def preserve_failed_staging(staged_path, final_path, logfile, reason):
    """Move a failed/unverified staged MKV out of the final filename namespace."""
    staged_path = Path(staged_path)
    if not staged_path.is_file() or staged_path.stat().st_size <= 0:
        return None
    failed_dir = final_path.parent / "Failed"
    failed_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    failed_path = failed_dir / "{}.failed-{}-{}.mkv".format(final_path.stem, stamp, os.getpid())
    counter = 1
    while failed_path.exists():
        failed_path = failed_dir / "{}.failed-{}-{}-{}.mkv".format(final_path.stem, stamp, os.getpid(), counter)
        counter += 1
    shutil.move(str(staged_path), str(failed_path))
    write_log(logfile, "未通過驗證的暫存輸出已移至 Failed，不佔用正式輸出名稱：{}；原因：{}".format(
        failed_path, reason))
    return failed_path


def is_english_language(value):
    """Recognize common ISO-639 and BCP-47 English language identifiers."""
    value = str(value or "").strip().lower().replace("_", "-")
    return value in {"eng", "en", "english"} or value.startswith("en-")


def is_chinese_language_properties(properties):
    """Recognize generic Chinese BCP-47 tags and their ISO 639-2 legacy forms."""
    properties = properties or {}
    ietf = str(properties.get("language_ietf") or "").strip().replace("_", "-").lower()
    legacy = str(properties.get("language") or "").strip().lower()
    if ietf:
        primary = ietf.split("-", 1)[0]
        return primary in {"zh", "zho", "chi"}
    return legacy in {"zh", "zho", "chi"}


def is_english_track_properties(properties):
    """Identify an English subtitle track from Matroska language tags or a clear track name."""
    properties = properties or {}
    name = str(properties.get("track_name") or "").strip()
    low = name.casefold()
    # A clearly bilingual track name must not be mistaken for an English-only subtitle,
    # even if its language metadata is inaccurate.
    bilingual_markers = ("chinese", "中文", "雙語", "双语", "bilingual", "中英")
    if any(marker in low for marker in bilingual_markers):
        return False
    if is_english_language(properties.get("language")):
        return True
    if is_english_language(properties.get("language_ietf")):
        return True
    if not name:
        return False
    return bool(re.match(r"^(english|英文|英語)(?:$|[\s_\-\[(])", name, re.IGNORECASE))


def is_target_language_properties(properties, target_language):
    """Recognize one selected language profile without mistaking bilingual tracks for it."""
    props = properties or {}
    name = str(props.get("track_name") or "").strip().casefold()
    bilingual_markers = ("chinese", "中文", "雙語", "双语", "bilingual", "中英")
    if any(marker in name for marker in bilingual_markers):
        return False
    resolved, _ = resolve_track_language(props)
    if resolved == target_language:
        return True
    names = {
        "eng": r"^(english|英文|英語)(?:$|[\s_\-\[(])",
        "jpn": r"^(japanese|日本語|日文|日語)(?:$|[\s_\-\[(])",
        "kor": r"^(korean|한국어|韓國語|韓文|韓語)(?:$|[\s_\-\[(])",
    }
    pattern = names.get(target_language)
    return bool(pattern and re.match(pattern, name, re.IGNORECASE))


def choose_primary_default_target(source_sub_tracks, new_language, new_name, target_language="eng"):
    """Return (source_track_id, external_is_default, diagnostic) for EN/JA/KO.

    If several candidates exist, use exactly one source Default track; otherwise fail closed.
    """
    source_candidates = [t for t in source_sub_tracks
                         if is_target_language_properties(t.get("properties", {}), target_language)]
    external_is_target = is_target_language_properties({"language": new_language, "track_name": new_name},
                                                       target_language)
    candidate_count = len(source_candidates) + (1 if external_is_target else 0)
    target_name = {"eng": "English", "jpn": "Japanese", "kor": "Korean"}.get(target_language, target_language)
    if candidate_count == 0:
        return None, False, "找不到可識別的{}字幕：來源 MKV 及本次外掛字幕均沒有對應語言標籤。請確認字幕語言 metadata。".format(target_name)
    if candidate_count == 1:
        if source_candidates:
            return source_candidates[0].get("id"), False, "唯一{}字幕位於來源 MKV 內。".format(target_name)
        return None, True, "唯一{}字幕是本次新增的外掛字幕。".format(target_name)

    already_default = [t for t in source_candidates
                       if t.get("properties", {}).get("default_track") is True]
    if len(already_default) == 1:
        return already_default[0].get("id"), False, "找到多條{}字幕；沿用來源中唯一已設為 Default 的字幕。".format(target_name)
    return None, False, "找到多條{}字幕，但沒有唯一一條來源 Default 字幕；為避免選錯版本，停止該集。".format(target_name)



def validate_subtitle_source(ffprobe, subtitle_path):
    # Read all subtitle packets and require one non-empty subtitle stream.
    cmd = [
        ffprobe, "-v", "error", "-count_packets",
        "-show_entries", "stream=index,codec_type,codec_name,nb_read_packets",
        "-of", "json", str(subtitle_path)
    ]
    proc = run_text(cmd)
    if proc.returncode != 0:
        raise RuntimeError("字幕檔無法解析：{}".format((proc.stderr or proc.stdout)[-2000:]))
    try:
        info = json.loads(proc.stdout)
    except Exception as exc:
        raise RuntimeError("字幕檔資訊無法解析：{}".format(exc))
    streams = info.get("streams", [])
    if len(streams) != 1 or streams[0].get("codec_type") != "subtitle":
        raise RuntimeError("字幕檔未被識別為恰好一條字幕軌（識別到 {} 條）。".format(len(streams)))
    try:
        packet_count = int(streams[0].get("nb_read_packets", 0))
    except (TypeError, ValueError):
        packet_count = 0
    if packet_count < 1:
        raise RuntimeError("字幕檔沒有可讀取的字幕事件（0 個字幕封包）。")
    return streams[0], packet_count, proc.stderr

def validate_source(ffprobe, mkvmerge, video):
    # Both tools must be able to inspect the source; require at least one video and audio stream.
    pinfo, p_stderr = ffprobe_info(ffprobe, video)
    counts = stream_counts(pinfo)
    if counts.get("video", 0) < 1:
        raise RuntimeError("來源影片沒有可辨識的影片軌，已停止封裝。")
    if counts.get("audio", 0) < 1:
        raise RuntimeError("來源影片沒有音訊軌，已停止封裝。")
    minfo, mrc, m_stderr = mkvmerge_info(mkvmerge, video)
    mcounts = mkv_track_counts(minfo)
    if mcounts.get("video", 0) < 1:
        raise RuntimeError("mkvmerge 未辨識到來源影片軌，已停止封裝。")
    if mcounts.get("audio", 0) < 1:
        raise RuntimeError("mkvmerge 未辨識到來源音訊軌，已停止封裝。")
    return pinfo, minfo, p_stderr, m_stderr, mrc


def validate_subtitle_stream_track(ffmpeg, ffprobe, media_path, subtitle_ordinal, stream, logfile, label):
    """Validate text subtitle parsing or bitmap subtitle packet readability without writing media files."""
    codec = str((stream or {}).get("codec_name") or "").lower()
    bitmap_codecs = {"hdmv_pgs_subtitle", "dvd_subtitle", "dvb_subtitle", "xsub"}
    if codec in bitmap_codecs:
        cmd = [ffprobe, "-v", "error", "-select_streams", "s:{}".format(subtitle_ordinal),
               "-count_packets", "-show_entries", "stream=codec_name,nb_read_packets", "-of", "json", str(media_path)]
        proc = run_text(cmd)
        if proc.returncode != 0:
            if proc.stderr:
                write_block(logfile, "{} FFprobe 字幕封包檢查輸出".format(label), proc.stderr)
            return False, "FFprobe 無法讀取 bitmap 字幕軌（{}）。".format(codec), False
        try:
            data = json.loads(proc.stdout)
            found = data.get("streams", [])
            packets = int(found[0].get("nb_read_packets", 0)) if found else 0
        except Exception:
            packets = 0
        if packets <= 0:
            return False, "bitmap 字幕軌（{}）沒有可讀封包。".format(codec), False
        write_log(logfile, "{}：bitmap 字幕 {}，FFprobe 讀取 {} 個封包。".format(label, codec, packets))
        return True, "已讀取 {} 個 bitmap 字幕封包".format(packets), False

    # Send the ASS validation muxer to stdout instead of /dev/null. On macOS and
    # current FFmpeg builds, /dev/null already exists; FFmpeg may print
    # "File '/dev/null' already exists. Exiting." and even return code 0, which
    # used to be misread as successful subtitle parsing. A pipe target avoids
    # filesystem output entirely. stdout is discarded by the parent process.
    cmd = [ffmpeg, "-hide_banner", "-nostdin", "-v", "warning", "-xerror",
           "-i", str(media_path), "-map", "0:s:{}".format(subtitle_ordinal),
           "-c:s", "ass", "-f", "ass", "pipe:1"]
    proc = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                          text=True, encoding="utf-8", errors="replace")
    diagnostic = (proc.stderr or "").strip()
    if diagnostic:
        write_block(logfile, "{} FFmpeg 字幕解析輸出".format(label), diagnostic)
    fatal_markers = (
        "error opening output", "error while decoding", "invalid data found",
        "could not find codec parameters", "error submitting packet",
        "failed to read frame", "conversion failed",
    )
    fatal_diagnostic = any(marker in diagnostic.casefold() for marker in fatal_markers)
    if proc.returncode != 0 or fatal_diagnostic:
        return False, "FFmpeg 無法解析字幕軌（{}），退出碼 {}。".format(codec or "未知編碼", proc.returncode), bool(diagnostic)
    return True, "文字字幕解析通過（{}）".format(codec or "未知編碼"), bool(diagnostic)


def verify_output(mkvmerge, ffmpeg, ffprobe, out_path, source_probe_info, logfile,
                  second_track_name="Chinese + English", second_language="und", mkvextract=None,
                  primary_track_name="English", primary_language="eng"):
    # Check MKV track metadata and required subtitle flags.
    minfo, mrc, m_stderr = mkvmerge_info(mkvmerge, out_path)
    if m_stderr:
        write_block(logfile, "mkvmerge -J stderr", m_stderr)
    if mrc == 1:
        write_log(logfile, "警告：mkvmerge -J 回傳警告代碼 1。")

    tracks = minfo.get("tracks", [])
    counts = mkv_track_counts(minfo)
    source_counts = mkv_track_counts({"tracks": source_probe_info.get("_mkvmerge_tracks", [])})
    if counts.get("video", 0) < 1:
        return False, "輸出檔沒有影片軌。", []
    if counts.get("audio", 0) < 1:
        return False, "輸出檔沒有音訊軌。", []
    if counts.get("video", 0) != source_counts.get("video", 0):
        return False, "輸出影片軌數量與來源不一致（來源 {}、輸出 {}）。".format(source_counts.get("video", 0), counts.get("video", 0)), []
    if counts.get("audio", 0) != source_counts.get("audio", 0):
        return False, "輸出音訊軌數量與來源不一致（來源 {}、輸出 {}）。".format(source_counts.get("audio", 0), counts.get("audio", 0)), []

    subs = [t for t in tracks if t.get("type") == "subtitles"]
    if len(subs) != 2:
        return False, "字幕軌道數量不是 2（實際：{}）。".format(len(subs)), []
    props = [t.get("properties", {}) for t in subs]
    primary = [p for p in props if p.get("track_name") == primary_track_name and
               resolve_track_language(p)[0] == primary_language]
    if second_language == "zh":
        second = [p for p in props if p.get("track_name") == second_track_name and is_chinese_language_properties(p)]
    elif normalize_ietf_language_tag(second_language).startswith("zh-"):
        expected_ietf = normalize_ietf_language_tag(second_language)
        second = [p for p in props if p.get("track_name") == second_track_name
                  and is_chinese_language_properties(p)
                  and normalize_ietf_language_tag(p.get("language_ietf")) == expected_ietf]
    else:
        second = [p for p in props if p.get("track_name") == second_track_name and p.get("language") == second_language]
    if len(primary) != 1 or len(second) != 1:
        return False, "預設語言／第二字幕軌道名稱或語言標記不符合預期（主字幕 {} / {}；第二軌 {} / {}）。".format(
            primary_track_name, primary_language, second_track_name, second_language), []
    if primary[0].get("default_track") is not True:
        return False, "預設語言字幕沒有設為 Default。", []
    if second[0].get("default_track") is not False:
        return False, "第二條字幕沒有設為非 Default。", []
    if primary[0].get("forced_track") is True or second[0].get("forced_track") is True:
        return False, "新增字幕不應標記為 Forced；請檢查封裝旗標。", []

    # Check stream counts and codecs with ffprobe as an independent parser.
    out_probe, p_stderr = ffprobe_info(ffprobe, out_path)
    if p_stderr:
        write_block(logfile, "ffprobe stderr", p_stderr)
    source_mkv_info = source_probe_info.get("_mkvmerge_info")
    source_path = source_probe_info.get("_source_path")
    if not source_mkv_info or not source_path:
        return False, "缺少來源 MKVToolNix／路徑快照，無法完成受保護 metadata 比對。", []
    allow_implicit_default_duration = False
    verified_leading_video_packet_drop = False
    if needs_implicit_default_duration_check(source_mkv_info, minfo):
        output_video = _mkv_tracks(minfo, "video")[0]
        output_default_duration = (output_video.get("properties", {}) or {}).get("default_duration")
        try:
            source_timing = ffprobe_video_packet_timing(ffprobe, source_path)
            output_timing = ffprobe_video_packet_timing(ffprobe, out_path)
        except Exception as exc:
            return False, "最終 MKV DefaultDuration 正規化驗證失敗，無法讀取完整影格時序：{}。".format(exc), []

        source_video_packets = source_timing.get("packets", [])
        output_video_packets = output_timing.get("packets", [])
        decoded_frames_identical = False
        if len(source_video_packets) != len(output_video_packets):
            prefix_count, prefix_error = leading_keyframe_packet_drop_candidate(
                source_timing, output_timing)
            if prefix_count is None or prefix_count <= 0:
                return False, ("最終 MKV DefaultDuration 正規化拒絕：影片封包數不同，且{}"
                               .format(prefix_error)), []
            # Skip only the same number of leading source decode records as the
            # independently verified packet prefix. FFmpeg can emit concealment
            # frames for undecodable pre-keyframe H.264/HEVC packets; comparing the
            # entire source decode against an MKV that correctly discarded them
            # would create a false frame-count mismatch. The retained suffix still
            # has to match output frame-for-frame, including MD5 and timing.
            frame_ok, frame_message = compare_decoded_video_frame_sequences(
                ffmpeg, source_path, out_path, context="最終 MKV ",
                skip_source_prefix=prefix_count)
            if not frame_ok:
                return False, "最終 MKV DefaultDuration 正規化拒絕：影片封包數不同，且{}".format(
                    frame_message), []
            decoded_frames_identical = True
            write_log(logfile, frame_message)

        timing_ok, timing_message, leading_drop_count = compare_implicit_default_duration_timing(
            source_timing, output_timing, output_default_duration,
            context="最終 MKV ", decoded_frames_identical=decoded_frames_identical)
        if not timing_ok:
            return False, timing_message, []
        allow_implicit_default_duration = True
        verified_leading_video_packet_drop = leading_drop_count > 0
        write_log(logfile, timing_message)

    mkv_ok, mkv_message, mkv_notes = compare_mkv_track_metadata(
        source_mkv_info, minfo, ("video", "audio"), context="最終 MKV ",
        allow_implicit_default_duration=allow_implicit_default_duration)
    if not mkv_ok:
        return False, mkv_message, []
    chapters_ok, chapters_message = compare_chapters_and_attachments(
        source_mkv_info, minfo, context="最終 MKV ")
    if not chapters_ok:
        return False, chapters_message, []
    attachment_ok, attachment_message = compare_mkv_attachment_payloads(
        mkvextract, source_path, source_mkv_info, out_path, minfo, logfile)
    if not attachment_ok:
        return False, attachment_message, []
    write_log(logfile, chapters_message)
    write_log(logfile, attachment_message)
    for note in mkv_notes:
        write_log(logfile, note)

    ff_ok, ff_message, ff_notes = compare_ffprobe_stream_metadata(
        source_probe_info, out_probe, ("video", "audio"), context="最終 MKV ",
        allow_implicit_single_video_default=has_implicit_single_video_default_promotion(source_mkv_info, minfo))
    if not ff_ok:
        return False, ff_message, []
    for note in ff_notes:
        write_log(logfile, note)
    # If MKVToolNix removed only the undecodable prefix before the first H.264/HEVC
    # keyframe, the independent full decoded-frame MD5/timestamp comparison above is
    # stronger than packet-count equality for the video stream. Audio remains strict.
    packet_kinds = ("audio",) if verified_leading_video_packet_drop else ("video", "audio")
    packet_ok, packet_message = compare_ffprobe_packet_counts(
        ffprobe, source_path, out_path, packet_kinds, context="最終 MKV ", logfile=logfile,
        source_audio_stream_indices=source_probe_info.get("_audio_source_stream_indices"))
    if not packet_ok:
        return False, packet_message, []
    write_log(logfile, packet_message)

    src_counts = stream_counts(source_probe_info)
    out_counts = stream_counts(out_probe)
    for kind in ("video", "audio"):
        if out_counts.get(kind, 0) != src_counts.get(kind, 0):
            return False, "ffprobe 檢查：{}軌數量與來源不一致（來源 {}、輸出 {}）。".format(
                "影片" if kind == "video" else "音訊", src_counts.get(kind, 0), out_counts.get(kind, 0)), []
        if stream_codec_counts(out_probe, kind) != stream_codec_counts(source_probe_info, kind):
            return False, "ffprobe 檢查：輸出{}編碼與來源不一致。".format("影片" if kind == "video" else "音訊"), []
    if out_counts.get("subtitle", 0) != 2:
        return False, "ffprobe 檢查：字幕軌數量不是 2（實際：{}）。".format(out_counts.get("subtitle", 0)), []

    # Validate text subtitle decoding; bitmap subtitles are checked by reading/counting packets.
    # This verifies readability, not translation accuracy or audio/subtitle synchronization.
    subtitle_warnings = []
    subtitle_streams = [x for x in out_probe.get("streams", []) if x.get("codec_type") == "subtitle"]
    for sub_index, sub_stream in enumerate(subtitle_streams):
        sub_ok, sub_message, sub_has_diagnostic = validate_subtitle_stream_track(
            ffmpeg, ffprobe, out_path, sub_index, sub_stream, logfile, "輸出字幕 {}".format(sub_index + 1))
        if not sub_ok:
            return False, sub_message, []
        if sub_has_diagnostic:
            subtitle_warnings.append("字幕 {} 有 FFmpeg 診斷訊息".format(sub_index + 1))

    # Full decode test: decode all video/audio streams to the null muxer. No output file or re-encode.
    decode_cmd = [
        ffmpeg, "-hide_banner", "-nostdin", "-v", "warning", "-xerror", "-i", str(out_path),
        "-map", "0:v?", "-map", "0:a?", "-f", "null", "-"
    ]
    dec = subprocess.run(decode_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                         text=True, encoding="utf-8", errors="replace")
    if dec.stderr:
        write_block(logfile, "FFmpeg 完整解碼輸出", dec.stderr)
    if dec.returncode != 0:
        return False, "FFmpeg 完整解碼失敗，退出碼 {}。詳見日誌。".format(dec.returncode), []

    warnings = []
    if mrc == 1:
        warnings.append("輸出 MKV 的 mkvmerge -J 有警告，詳見日誌")
    if source_probe_info.get("_source_mkvmerge_warning"):
        warnings.append("來源檔案的 mkvmerge 識別過程有警告，詳見日誌")
    if dec.stderr.strip():
        warnings.append("FFmpeg 影音解碼過程有警告訊息，詳見日誌")
    if subtitle_warnings:
        warnings.extend(subtitle_warnings)
    if second_language == "und":
        second_description = "雙語備用"
    elif normalize_ietf_language_tag(second_language).startswith("zh-"):
        second_description = second_track_name + " 備用"
    elif second_language == "eng" and "sdh" in second_track_name.casefold():
        second_description = "SDH 英文備用"
    else:
        second_description = "中文字幕備用"
    detail = "驗證通過：影片 {} 軌、保留音訊 {} 軌、字幕 2 軌；影音完整解碼及兩條字幕解析通過；{} 預設、{}".format(
        counts.get("video", 0), counts.get("audio", 0), primary_track_name, second_description)
    if warnings:
        detail += "；另有警告，詳見日誌"
    return True, detail, warnings



def validate_append_output(mkvmerge, ffmpeg, ffprobe, output, source_probe, source_mkv,
                           mode, new_language, new_name, logfile, selected_default_source_id=None,
                           external_is_default=False, mkvextract=None, primary_language="eng"):
    """Validate an MKV after appending one external subtitle without transcoding."""
    out_mkv, mrc, m_stderr = mkvmerge_info(mkvmerge, output)
    if m_stderr:
        write_block(logfile, "輸出 mkvmerge -J stderr", m_stderr)
    out_tracks = out_mkv.get("tracks", [])
    source_tracks = source_mkv.get("tracks", [])
    src_sub_tracks = [t for t in source_tracks if t.get("type") == "subtitles"]
    out_sub_tracks = [t for t in out_tracks if t.get("type") == "subtitles"]
    if len(out_sub_tracks) != len(src_sub_tracks) + 1:
        return False, "輸出字幕軌數量不符：來源 {} 條，預期輸出 {} 條，實際 {} 條。".format(
            len(src_sub_tracks), len(src_sub_tracks) + 1, len(out_sub_tracks)), []

    # Strictly compare original A/V and subtitle metadata; Default is excluded here
    # because modes 2 and 3 intentionally change it and validate it below.
    output_original_tracks = [t for t in out_tracks if t.get("type") != "subtitles"] + out_sub_tracks[:-1]
    source_original_view = {"tracks": source_tracks,
                            "chapters": source_mkv.get("chapters", []),
                            "attachments": source_mkv.get("attachments", [])}
    output_original_view = {"tracks": output_original_tracks,
                            "chapters": out_mkv.get("chapters", []),
                            "attachments": out_mkv.get("attachments", [])}
    mkv_ok, mkv_message, mkv_notes = compare_mkv_track_metadata(
        source_original_view, output_original_view, ("video", "audio", "subtitles"),
        context="追加字幕後 ", ignored_fields={"subtitles": ("default_track",)})
    if not mkv_ok:
        return False, mkv_message, []
    for note in mkv_notes:
        write_log(logfile, note)
    asset_ok, asset_message = compare_chapters_and_attachments(source_mkv, out_mkv, context="追加字幕後 ")
    if not asset_ok:
        return False, asset_message, []
    attachment_ok, attachment_message = compare_mkv_attachment_payloads(
        mkvextract, source_mkv.get("_source_path"), source_mkv, output, out_mkv, logfile)
    if not attachment_ok:
        return False, attachment_message, []
    write_log(logfile, asset_message)
    write_log(logfile, attachment_message)

    src_mkv_counts = mkv_track_counts(source_mkv)
    out_mkv_counts = mkv_track_counts(out_mkv)
    for kind, label in (("video", "影片"), ("audio", "音訊")):
        if src_mkv_counts.get(kind, 0) != out_mkv_counts.get(kind, 0):
            return False, "輸出{}軌數量與來源不一致（來源 {}、輸出 {}）。".format(
                label, src_mkv_counts.get(kind, 0), out_mkv_counts.get(kind, 0)), []

    # Existing subtitle tracks must remain in the same order with their identity metadata.
    selected_source_index = None
    for idx, src_track in enumerate(src_sub_tracks):
        src_prop = src_track.get("properties", {})
        out_prop = out_sub_tracks[idx].get("properties", {})
        for field, label in (("language", "語言標記"), ("track_name", "軌道名稱")):
            if src_prop.get(field) != out_prop.get(field):
                return False, "原有第 {} 條字幕的{}發生變化。".format(idx + 1, label), []

        if mode == "append_preserve":
            expected = src_prop.get("default_track")
            if not isinstance(expected, bool) or out_prop.get("default_track") is not expected:
                return False, "原有第 {} 條字幕的 Default 旗標未能原樣保留。".format(idx + 1), []
        elif mode == "append_english_auto_default":
            expected = (src_track.get("id") == selected_default_source_id)
            if out_prop.get("default_track") is not expected:
                return False, "原有第 {} 條字幕的 Default 旗標與所選預設語言不符。".format(idx + 1), []
            if expected:
                selected_source_index = idx
        elif mode == "append_english_default":
            if out_prop.get("default_track") is not False:
                return False, "原有第 {} 條字幕未被設為非 Default。".format(idx + 1), []

    new_prop = out_sub_tracks[-1].get("properties", {})
    if new_prop.get("language") != new_language:
        return False, "新增字幕語言標記不符（預期 {}，實際 {}）。".format(
            new_language, new_prop.get("language")), []
    if new_prop.get("track_name") != new_name:
        return False, "新增字幕軌道名稱不符（預期 {}，實際 {}）。".format(
            new_name, new_prop.get("track_name")), []

    if mode == "append_preserve":
        expected_new_default = False
    elif mode == "append_english_default":
        expected_new_default = True
    else:
        expected_new_default = external_is_default
    if new_prop.get("default_track") is not expected_new_default:
        return False, "新增字幕 Default 旗標不符（預期 {}）。".format(expected_new_default), []

    if mode == "append_english_auto_default":
        default_tracks = [t for t in out_sub_tracks if t.get("properties", {}).get("default_track") is True]
        if len(default_tracks) != 1:
            return False, "輸出字幕沒有恰好一條 Default 軌（實際 {} 條）。".format(len(default_tracks)), []
        default_props = default_tracks[0].get("properties", {})
        if resolve_track_language(default_props)[0] != primary_language:
            return False, "輸出 Default 字幕不是所選的預設語言（{}）。".format(primary_language), []

    out_probe, p_stderr = ffprobe_info(ffprobe, output)
    if p_stderr:
        write_block(logfile, "輸出 ffprobe stderr", p_stderr)
    source_original_probe = {"streams": list(source_probe.get("streams", []))}
    original_sub_count = sum(1 for x in source_probe.get("streams", []) if x.get("codec_type") == "subtitle")
    output_original_streams = [x for x in out_probe.get("streams", []) if x.get("codec_type") != "subtitle"]
    output_original_streams.extend([x for x in out_probe.get("streams", []) if x.get("codec_type") == "subtitle"][:original_sub_count])
    ff_ok, ff_message, ff_notes = compare_ffprobe_stream_metadata(
        source_original_probe, {"streams": output_original_streams},
        ("video", "audio", "subtitle"), context="追加字幕後 ", check_disposition=False,
        allow_implicit_single_video_default=has_implicit_single_video_default_promotion(source_mkv, out_mkv))
    if not ff_ok:
        return False, ff_message, []
    for note in ff_notes:
        write_log(logfile, note)
    try:
        source_packets = ffprobe_packet_summary(ffprobe, source_mkv.get("_source_path"))
        output_packets = ffprobe_packet_summary(ffprobe, output)
        for kind in ("audio", "subtitle"):
            src_items = source_packets.get(kind, [])
            if kind == "audio" and source_probe.get("_audio_source_stream_indices") is not None:
                selected = {int(x) for x in source_probe.get("_audio_source_stream_indices", [])}
                src_items = [x for x in src_items if x[2] is not None and int(x[2]) in selected]
            out_items = output_packets.get(kind, [])[:len(src_items)] if kind == "subtitle" else output_packets.get(kind, [])
            if len(src_items) != len(out_items):
                return False, "追加字幕後{}封包軌道數量與來源不同。".format(kind), []
            for idx, (src_item, out_item) in enumerate(zip(src_items, out_items), 1):
                if src_item[0] != out_item[0]:
                    return False, "原有第 {} 條{}封包 codec 不一致。".format(idx, "音訊" if kind == "audio" else "字幕"), []
                if src_item[1] is not None and out_item[1] is not None and src_item[1] != out_item[1]:
                    return False, "原有第 {} 條{}封包數不同（來源 {}、輸出 {}）。".format(
                        idx, "音訊" if kind == "audio" else "字幕", src_item[1], out_item[1]), []
                if src_item[1] is None or out_item[1] is None:
                    write_log(logfile, "原有第 {} 條{}無法取得完整封包數，將依完整解碼／字幕解析結果判斷。".format(
                        idx, "音訊" if kind == "audio" else "字幕"))
    except Exception as exc:
        return False, "追加字幕後無法比較來源／輸出封包數：{}".format(exc), []

    src_counts = stream_counts(source_probe)
    out_counts = stream_counts(out_probe)
    for kind, label in (("video", "影片"), ("audio", "音訊")):
        if src_counts.get(kind, 0) != out_counts.get(kind, 0):
            return False, "FFprobe：輸出{}軌數量與來源不一致。".format(label), []
        if stream_codec_counts(source_probe, kind) != stream_codec_counts(out_probe, kind):
            return False, "FFprobe：輸出{}編碼與來源不一致。".format(label), []
    src_sub_streams = [x for x in source_probe.get("streams", []) if x.get("codec_type") == "subtitle"]
    out_sub_streams = [x for x in out_probe.get("streams", []) if x.get("codec_type") == "subtitle"]
    if len(out_sub_streams) != len(src_sub_streams) + 1:
        return False, "FFprobe：輸出字幕串流數量不符。", []
    for idx, src_stream in enumerate(src_sub_streams):
        if src_stream.get("codec_name") != out_sub_streams[idx].get("codec_name"):
            return False, "FFprobe：原有第 {} 條字幕編碼發生變化。".format(idx + 1), []

    subtitle_warnings = []
    for sub_index, sub_stream in enumerate(out_sub_streams):
        sub_ok, sub_message, sub_has_diagnostic = validate_subtitle_stream_track(
            ffmpeg, ffprobe, output, sub_index, sub_stream, logfile, "輸出字幕 {}".format(sub_index + 1))
        if not sub_ok:
            return False, sub_message, []
        if sub_has_diagnostic:
            subtitle_warnings.append("字幕 {} 有 FFmpeg 診斷訊息".format(sub_index + 1))

    decode_cmd = [ffmpeg, "-hide_banner", "-nostdin", "-v", "warning", "-xerror",
                  "-i", str(output), "-map", "0:v?", "-map", "0:a?", "-f", "null", "-"]
    dec = subprocess.run(decode_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                         text=True, encoding="utf-8", errors="replace")
    if dec.stderr.strip():
        write_block(logfile, "FFmpeg 完整影音解碼輸出", dec.stderr)
    if dec.returncode != 0:
        return False, "FFmpeg 完整影音解碼失敗，退出碼 {}。".format(dec.returncode), []

    warnings = []
    if mrc == 1:
        warnings.append("mkvmerge -J 回傳警告代碼 1，詳見日誌")
    if subtitle_warnings:
        warnings.extend(subtitle_warnings)
    if dec.stderr.strip():
        warnings.append("FFmpeg 影音解碼有診斷訊息，詳見日誌")

    if mode == "append_preserve":
        desc = "新增 1 條字幕；原有 {} 條字幕及 Default 旗標保持不變".format(len(src_sub_tracks))
    elif mode == "append_english_default":
        desc = "新增所選主要語言字幕設為 Default；原有 {} 條字幕設為非 Default".format(len(src_sub_tracks))
    else:
        if external_is_default:
            chosen_desc = "本次新增的所選主要語言字幕"
        else:
            chosen_desc = "來源 MKV 內嵌的目標語言字幕（第 {} 條原字幕）".format((selected_source_index or 0) + 1)
        desc = "新增 1 條字幕；{} 設為唯一 Default，其餘字幕設為非 Default".format(chosen_desc)
    msg = "驗證通過：影片 {} 軌、音訊 {} 軌、字幕 {} 條；{}；影音完整解碼及全部字幕解析通過。".format(
        out_mkv_counts.get("video", 0), out_mkv_counts.get("audio", 0), len(out_sub_tracks), desc)
    if warnings:
        msg += "另有診斷訊息，詳見日誌。"
    return True, msg, warnings


def mux_append_one(mkvmerge, mkvextract, ffmpeg, ffprobe, video, subtitle, output, logfile,
                   mode, new_language, new_name, primary_language="eng"):
    """Append one external subtitle to an MKV. Never re-encodes or overwrites source."""
    try:
        sub_info, packet_count, sub_stderr = validate_subtitle_source(ffprobe, subtitle)
        write_log(logfile, "外掛字幕來源檢查：{}，{} 個字幕事件。".format(
            sub_info.get("codec_name", "未知格式"), packet_count))
        if sub_stderr:
            write_block(logfile, "外掛字幕 ffprobe stderr", sub_stderr)
        source_probe, source_mkv, source_probe_stderr, source_mkv_stderr, source_mkv_rc = validate_source(
            ffprobe, mkvmerge, video)
        if source_probe_stderr:
            write_block(logfile, "來源 ffprobe stderr", source_probe_stderr)
        if source_mkv_stderr:
            write_block(logfile, "來源 mkvmerge -J stderr", source_mkv_stderr)
        write_log(logfile, "來源檢查：影片 {} 軌、音訊 {} 軌、內嵌字幕 {} 條。".format(
            stream_counts(source_probe).get("video", 0),
            stream_counts(source_probe).get("audio", 0),
            mkv_track_counts(source_mkv).get("subtitles", 0)))
        if source_mkv_rc == 1:
            write_log(logfile, "來源 mkvmerge -J 回傳警告代碼 1；原始診斷輸出已保存。")
        source_probe["_source_path"] = str(video)
        source_mkv["_source_path"] = str(video)
        audio_plan = build_audio_language_plan(source_mkv, primary_language)
        log_audio_language_plan(logfile, audio_plan)
        source_probe, source_mkv = filtered_source_views(source_probe, source_mkv, audio_plan)
    except Exception as exc:
        return False, "來源或外掛字幕檢查失敗：{}".format(exc)

    source_mkv["_source_path"] = str(video)
    source_sub_tracks = [t for t in source_mkv.get("tracks", []) if t.get("type") == "subtitles"]
    selected_default_source_id = None
    external_is_default = False
    if mode == "append_english_auto_default":
        selected_default_source_id, external_is_default, diagnostic = choose_primary_default_target(
            source_sub_tracks, new_language, new_name, primary_language)
        write_log(logfile, "預設語言字幕選擇：" + diagnostic)
        if selected_default_source_id is None and not external_is_default:
            return False, diagnostic

    cmd = [mkvmerge, "--output", str(output)] + audio_language_cli_options(audio_plan)
    if mode == "append_english_default":
        # Mode 2: force the newly appended selected-language subtitle to be the only default.
        for track in source_sub_tracks:
            cmd.extend(["--default-track-flag", "{}:no".format(track.get("id"))])
    elif mode == "append_english_auto_default":
        # Mode 3: exactly one selected-language subtitle (embedded OR newly added) is default.
        for track in source_sub_tracks:
            is_selected = (track.get("id") == selected_default_source_id)
            cmd.extend(["--default-track-flag", "{}:{}".format(
                track.get("id"), "yes" if is_selected else "no")])
    else:
        # Mode 4: explicitly reapply every existing subtitle Default flag.
        for track in source_sub_tracks:
            default_flag = track.get("properties", {}).get("default_track")
            if not isinstance(default_flag, bool):
                return False, "無法確認來源 MKV 的既有字幕 Default 旗標；為避免改動原設定，已停止封裝。"
            cmd.extend(["--default-track-flag", "{}:{}".format(
                track.get("id"), "yes" if default_flag else "no")])
    cmd.append(str(video))
    appended_default = (mode == "append_english_default" or
                         (mode == "append_english_auto_default" and external_is_default))
    cmd.extend(["--language", "0:{}".format(new_language),
                "--track-name", "0:{}".format(new_name),
                "--default-track-flag", "0:{}".format("yes" if appended_default else "no"),
                "--forced-display-flag", "0:no", str(subtitle)])

    if mode == "append_english_auto_default":
        if external_is_default:
            write_log(logfile, "最終預設字幕：本次新增的外掛字幕（{} / {}）。".format(new_language, new_name))
        else:
            chosen = next((t for t in source_sub_tracks if t.get("id") == selected_default_source_id), {})
            chosen_props = chosen.get("properties", {})
            write_log(logfile, "最終預設字幕：來源內嵌軌 ID {}，名稱={!r}，language={!r}，language_ietf={!r}。".format(
                selected_default_source_id, chosen_props.get("track_name"), chosen_props.get("language"),
                chosen_props.get("language_ietf")))

    proc = run_text(cmd)
    mux_output = "\n".join(x for x in [proc.stdout, proc.stderr] if x).strip()
    if mux_output:
        write_block(logfile, "mkvmerge 封裝完整輸出（{}）".format(output.name), mux_output)
    write_log(logfile, "mkvmerge 退出碼：{}（0=無警告；1=成功但有警告；2=錯誤）".format(proc.returncode))
    if proc.returncode not in (0, 1):
        return False, "mkvmerge 封裝失敗，退出碼 {}；詳見日誌。".format(proc.returncode)
    if not output.exists() or output.stat().st_size == 0:
        return False, "沒有產生有效輸出檔。"
    ok, message, warnings = validate_append_output(
        mkvmerge, ffmpeg, ffprobe, output, source_probe, source_mkv,
        mode, new_language, new_name, logfile, selected_default_source_id, external_is_default,
        mkvextract=mkvextract, primary_language=primary_language)
    if proc.returncode == 1:
        warnings.append("mkvmerge 封裝退出碼 1：成功但有警告；完整輸出已保存")
    if not ok:
        return False, message + " 未驗證檔會移至 Failed 供診斷，來源未被覆蓋。"
    if warnings:
        return True, message + "（有診斷訊息，詳見日誌）"
    return True, message

def english_subtitle_export_extension(track):
    """Return (extension, companion_extensions) for a Matroska subtitle codec."""
    props = track.get("properties", {}) or {}
    codec_id = str(props.get("codec_id") or track.get("codec") or "").upper()
    mapping = {
        "S_TEXT/ASS": (".ass", ()),
        "S_ASS": (".ass", ()),
        "S_TEXT/SSA": (".ssa", ()),
        "S_SSA": (".ssa", ()),
        "S_TEXT/UTF8": (".srt", ()),
        "S_TEXT/ASCII": (".srt", ()),
        "S_TEXT/USF": (".usf", ()),
        "S_TEXT/WEBVTT": (".vtt", ()),
        "S_HDMV/PGS": (".sup", ()),
        # mkvextract writes both the .idx index and its paired .sub bitmap file.
        "S_VOBSUB": (".idx", (".sub",)),
    }
    return mapping.get(codec_id)


def choose_english_track_for_export(mkv_info):
    """Choose one English track: unique track, or unique default English among several."""
    tracks = [t for t in mkv_info.get("tracks", []) if t.get("type") == "subtitles"]
    english = [t for t in tracks if is_english_track_properties(t.get("properties", {}))]
    if not english:
        return None, "輸出 MKV 中沒有可識別的英文字幕軌。"
    if len(english) == 1:
        return english[0], "唯一可識別的英文字幕軌。"
    defaults = [t for t in english if t.get("properties", {}).get("default_track") is True]
    if len(defaults) == 1:
        return defaults[0], "存在多條英文字幕，選取唯一一條已設為 Default 的英文軌。"
    return None, "輸出 MKV 有多條英文字幕，但沒有唯一的英文 Default 軌；為避免匯出錯誤版本，未擅自選擇。"


def choose_language_track_for_export(mkv_info, target_language="eng"):
    """Choose a subtitle in target language; prefer a unique Default when there are several."""
    tracks = [t for t in mkv_info.get("tracks", []) if t.get("type") == "subtitles"]
    matches = [t for t in tracks if is_target_language_properties(
        t.get("properties", {}), target_language)]
    if not matches:
        return None, "輸出 MKV 中沒有可識別的 {} 字幕軌。".format(target_language)
    if len(matches) == 1:
        return matches[0], "唯一可識別的 {} 字幕軌。".format(target_language)
    defaults = [t for t in matches if t.get("properties", {}).get("default_track") is True]
    if len(defaults) == 1:
        return defaults[0], "存在多條 {} 字幕，選取唯一 Default 軌。".format(target_language)
    return None, "輸出 MKV 有多條 {} 字幕，但沒有唯一 Default 軌；為避免匯出錯誤版本，未擅自選擇。".format(target_language)


def is_chinese_track_properties(properties):
    """Identify Chinese or explicitly Chinese-bilingual subtitle tracks without guessing."""
    props = properties or {}
    name = str(props.get("track_name") or "").strip().casefold()
    # A bilingual track can legitimately be tagged und or eng; an explicit Chinese
    # component in its title makes it eligible for the .zh sidecar.
    chinese_name = bool(re.search(
        r"中文|中英|chinese|mandarin|cantonese|普通话|普通話|国语|國語|粤语|粵語", name))
    try:
        resolved, _ = resolve_track_language(props)
    except RuntimeError:
        if chinese_name:
            raise
        return False
    if resolved == "zho":
        return True
    # Accept an explicit Chinese/bilingual title when the language tag is absent,
    # und, or English (the common legacy tag for Chinese+English bilingual tracks).
    return chinese_name and resolved in (None, "eng")


def choose_chinese_track_for_export(mkv_info):
    """Choose the unique Chinese/bilingual subtitle, or a unique Default among candidates."""
    tracks = [t for t in mkv_info.get("tracks", []) if t.get("type") == "subtitles"]
    matches = []
    conflicts = []
    for track in tracks:
        props = track.get("properties", {}) or {}
        try:
            if is_chinese_track_properties(props):
                matches.append(track)
        except RuntimeError as exc:
            conflicts.append("Track ID {} 名稱={!r}：{}".format(
                track.get("id"), props.get("track_name"), exc))
    if conflicts:
        return None, "中文／雙語候選字幕存在互相矛盾的語言標籤，為避免匯出錯誤版本，未匯出：" + "；".join(conflicts)
    if not matches:
        return None, "輸出 MKV 中沒有可識別的中文字幕／中英雙語字幕軌。"
    if len(matches) == 1:
        return matches[0], "唯一可識別的中文字幕／中英雙語字幕軌。"
    defaults = [t for t in matches if t.get("properties", {}).get("default_track") is True]
    if len(defaults) == 1:
        return defaults[0], "存在多條中文字幕／中英雙語字幕，選取唯一 Default 軌。"
    return None, "輸出 MKV 有多條中文字幕／中英雙語字幕，但沒有唯一 Default 軌；為避免匯出錯誤版本，未擅自選擇。"


def choose_english_sdh_track_for_export(mkv_info):
    """Choose the unique English track explicitly named/marked as SDH."""
    tracks = [t for t in mkv_info.get("tracks", []) if t.get("type") == "subtitles"]
    matches = []
    for track in tracks:
        props = track.get("properties", {}) or {}
        name = str(props.get("track_name") or "").casefold()
        if is_english_track_properties(props) and any(token in name for token in (
                "sdh", "hearing impaired", "closed caption", "captioned", "[cc]")):
            matches.append(track)
    if len(matches) == 1:
        return matches[0], "唯一標記為 SDH 的英文字幕軌。"
    if not matches:
        return None, "輸出 MKV 中沒有名稱明確標示 SDH 的英文字幕軌。"
    return None, "輸出 MKV 有多條 SDH 英文字幕，未能唯一選擇；未匯出。"


def export_english_sidecar(mkvextract, mkvmerge, ffprobe, output, logfile, sdh=False,
                           target_language="eng", chinese=False):
    """Extract the selected EN/JA/KO, Chinese/bilingual, or English SDH subtitle.

    Sidecar naming is <MKV basename>.en/.ja/.ko/.zh.<ext>; English SDH uses
    <MKV basename>.en.sdh.<ext>. Existing sidecar files are never overwritten.
    Returns (success, message, status).
    """
    try:
        if sdh and chinese:
            raise ValueError("SDH 英文匯出與中文字幕匯出不能同時指定。")
        info, rc, stderr = mkvmerge_info(mkvmerge, output)
        if stderr:
            write_block(logfile, "字幕旁掛匯出前 mkvmerge -J stderr", stderr)
        if rc == 1:
            write_log(logfile, "警告：字幕旁掛匯出前 mkvmerge -J 回傳警告代碼 1。")
        profile = next((v for v in LANGUAGE_PROFILES.values() if v["code"] == target_language),
                       LANGUAGE_PROFILES["1"])
        if sdh:
            track, reason = choose_english_sdh_track_for_export(info)
            sidecar_label, suffix, temp_prefix = "SDH 英文字幕", ".en.sdh", "frame-en-sdh-"
        elif chinese:
            track, reason = choose_chinese_track_for_export(info)
            sidecar_label, suffix, temp_prefix = "中文字幕／中文＋原語言雙語字幕", ".zh", "frame-zh-sub-"
            if track is not None:
                suffix = chinese_sidecar_suffix(track)
                track_name = str((track.get("properties", {}) or {}).get("track_name") or "")
                sidecar_label = ("中文＋原語言雙語字幕" if "+" in track_name or "＋" in track_name
                                 else "中文字幕")
        else:
            track, reason = choose_language_track_for_export(info, target_language)
            sidecar_label, suffix, temp_prefix = ("預設語言字幕（{}）".format(profile["name"]),
                                                     profile["suffix"], "frame-{}-sub-".format(profile["ietf"]))
        if track is None:
            write_log(logfile, sidecar_label + "旁掛檔未匯出：" + reason)
            return False, reason, "not_exported"
        codec = english_subtitle_export_extension(track)
        if codec is None:
            codec_id = (track.get("properties", {}) or {}).get("codec_id") or track.get("codec") or "未知編碼"
            reason = "目標語言字幕軌格式 {} 暫不支援旁掛匯出；MKV 內字幕仍保留。".format(codec_id)
            write_log(logfile, sidecar_label + "旁掛檔未匯出：" + reason)
            return False, reason, "unsupported"

        ext, companion_exts = codec
        track_id = track.get("id")
        if track_id is None:
            reason = "目標語言字幕軌缺少可用的 Matroska Track ID。"
            write_log(logfile, sidecar_label + "旁掛檔未匯出：" + reason)
            return False, reason, "error"

        base = output.parent / (output.stem + suffix)
        final_paths = [Path(str(base) + ext)] + [Path(str(base) + x) for x in companion_exts]
        existing = [str(p) for p in final_paths if p.exists()]
        if existing:
            reason = "旁掛字幕檔已存在，為避免覆蓋而跳過：" + "、".join(existing)
            write_log(logfile, sidecar_label + "旁掛檔未匯出：" + reason)
            return False, reason, "exists"

        props = track.get("properties", {}) or {}
        write_log(logfile, sidecar_label + "匯出選擇：Track ID {}；名稱={!r}；language={!r}；language_ietf={!r}；codec_id={!r}。{}".format(
            track_id, props.get("track_name"), props.get("language"), props.get("language_ietf"),
            props.get("codec_id"), reason))

        with tempfile.TemporaryDirectory(prefix=temp_prefix, dir=str(output.parent)) as temp_dir:
            temp_base = Path(temp_dir) / ("extracted" + ext)
            target_arg = "{}:{}".format(track_id, temp_base)
            proc = run_text([mkvextract, "tracks", str(output), target_arg])
            extract_output = "\n".join(x for x in [proc.stdout, proc.stderr] if x).strip()
            if extract_output:
                write_block(logfile, "mkvextract 目標語言字幕匯出輸出（{}）".format(output.name), extract_output)
            write_log(logfile, "mkvextract 退出碼：{}（0=成功；1=成功但有警告；2=錯誤）".format(proc.returncode))
            if proc.returncode not in (0, 1):
                reason = "mkvextract 匯出失敗，退出碼 {}；詳見日誌。".format(proc.returncode)
                write_log(logfile, sidecar_label + "旁掛檔未匯出：" + reason)
                return False, reason, "error"

            temp_paths = [temp_base] + [Path(str(Path(temp_dir) / "extracted") + x) for x in companion_exts]
            missing = [str(p) for p in temp_paths if not p.is_file() or p.stat().st_size <= 0]
            if missing:
                reason = "mkvextract 未產生完整且非空的字幕檔：" + "、".join(missing)
                write_log(logfile, sidecar_label + "旁掛檔未匯出：" + reason)
                return False, reason, "error"

            # Text subtitle formats are checked with FFprobe before being placed beside the MKV.
            if ext in {".ass", ".ssa", ".srt"}:
                try:
                    sub_info, packets, sub_stderr = validate_subtitle_source(ffprobe, temp_base)
                    write_log(logfile, "目標語言字幕旁掛檔驗證：{}，{} 個字幕事件。".format(
                        sub_info.get("codec_name", "未知格式"), packets))
                    if sub_stderr:
                        write_block(logfile, "目標語言字幕旁掛檔 ffprobe stderr", sub_stderr)
                except Exception as exc:
                    reason = "匯出的目標語言字幕未通過 FFprobe 驗證：{}".format(exc)
                    write_log(logfile, sidecar_label + "旁掛檔未匯出：" + reason)
                    return False, reason, "error"

            # Recheck target names immediately before moving; never replace a user's file.
            existing = [str(p) for p in final_paths if p.exists()]
            if existing:
                reason = "匯出期間發現同名旁掛字幕已出現，為避免覆蓋而取消：" + "、".join(existing)
                write_log(logfile, sidecar_label + "旁掛檔未匯出：" + reason)
                return False, reason, "exists"
            created_paths = []
            try:
                for temp_path, final_path in zip(temp_paths, final_paths):
                    # O_EXCL guarantees an existing file is never replaced, even in a race.
                    fd = os.open(str(final_path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
                    created_paths.append(final_path)
                    with temp_path.open("rb") as src, os.fdopen(fd, "wb") as dst:
                        shutil.copyfileobj(src, dst)
                        dst.flush()
                        os.fsync(dst.fileno())
            except Exception:
                for created_path in created_paths:
                    try:
                        created_path.unlink()
                    except OSError:
                        pass
                raise

        exported = "、".join(str(p) for p in final_paths)
        write_log(logfile, sidecar_label + "旁掛檔匯出成功：" + exported)
        return True, exported, "exported"
    except Exception as exc:
        label = "SDH 英文字幕" if sdh else "中文字幕／中英雙語字幕" if chinese else "預設語言字幕"
        reason = "{}旁掛檔匯出發生例外：{}".format(label, exc)
        write_log(logfile, reason)
        return False, reason, "error"


def export_chinese_sidecar(mkvextract, mkvmerge, ffprobe, output, logfile):
    """Export the uniquely identifiable Chinese or Chinese-bilingual subtitle as .zh."""
    return export_english_sidecar(
        mkvextract, mkvmerge, ffprobe, output, logfile, chinese=True)


def ffmpeg_encoder_names(ffmpeg):
    proc = run_text([ffmpeg, "-hide_banner", "-encoders"])
    text = (proc.stdout or "") + "\n" + (proc.stderr or "")
    names = set()
    for line in text.splitlines():
        m = re.match(r"\s*[A-Z\.]{6}\s+(\S+)", line)
        if m:
            names.add(m.group(1))
    return names


def ffmpeg_filter_names(ffmpeg):
    proc = run_text([ffmpeg, "-hide_banner", "-filters"])
    text = (proc.stdout or "") + "\n" + (proc.stderr or "")
    names = set()
    for line in text.splitlines():
        m = re.match(r"\s*[TSC\.]{3}\s+(\S+)", line)
        if m:
            names.add(m.group(1))
    return names


def ffprobe_first_frame_side_data(ffprobe, path):
    cmd = [ffprobe, "-v", "error", "-select_streams", "v:0", "-read_intervals", "%+#1",
           "-show_frames", "-show_entries", "frame=side_data_list", "-of", "json", str(path)]
    proc = run_text(cmd)
    if proc.returncode != 0:
        return []
    try:
        data = json.loads(proc.stdout)
        frames = data.get("frames", [])
        return frames[0].get("side_data_list", []) if frames else []
    except Exception:
        return []


def collect_video_side_data(stream, first_frame_side_data=None):
    items = list((stream or {}).get("side_data_list", []) or [])
    items += list(first_frame_side_data or [])
    return items


def detect_hdr_kind(ffprobe, path, stream):
    first_frame = ffprobe_first_frame_side_data(ffprobe, path)
    side = collect_video_side_data(stream, first_frame)
    descriptions = [str(x.get("side_data_type", "")) for x in side]
    codec_tag = str((stream or {}).get("codec_tag_string") or "").lower()
    profile = str((stream or {}).get("profile") or "").lower()
    dovi = (any("dolby vision" in x.lower() or "dovi" in x.lower() for x in descriptions)
            or codec_tag in {"dvhe", "dvh1"} or "dolby vision" in profile)
    hdr10plus = (any("2094-40" in x.lower() or "hdr10+" in x.lower() for x in descriptions)
                 or "hdr10+" in profile)
    transfer = str((stream or {}).get("color_transfer") or "").lower()
    if dovi:
        kind = "dolby_vision"
    elif hdr10plus:
        kind = "hdr10plus"
    elif transfer == "smpte2084":
        kind = "hdr10"
    elif transfer == "arib-std-b67":
        kind = "hlg"
    elif any("mastering display metadata" in x.lower() or "content light level metadata" in x.lower() for x in descriptions):
        kind = "hdr10"
    elif transfer and transfer not in {"unknown", "unspecified", "reserved"}:
        kind = "sdr_or_other"
    else:
        kind = "unknown"
    return kind, side, descriptions


def choose_encoding_settings(ffmpeg):
    encoders = ffmpeg_encoder_names(ffmpeg)
    print("\n" + "=" * 72)
    print("影片壓制設定（每個選項都附有說明；不會修改原始檔）")
    print("=" * 72)
    print("\n【影片編碼】")
    print("1. HEVC Main10（10-bit）（推薦）：畫質、容量及裝置支援較均衡，適合長期片庫。")
    print("2. HEVC 8-bit：較舊設備相容取向；不適合保留 HDR。")
    if "libsvtav1" in encoders or "libaom-av1" in encoders:
        print("3. AV1 10-bit：壓縮效率有機會更高，但編碼較慢，須確認播放設備支援。")
    else:
        print("3. AV1 10-bit：目前這個 FFmpeg 未編入可用 AV1 編碼器，無法選擇。")
    codec_choice = input("選擇 1、2 或 3（預設 1）：").strip() or "1"
    if codec_choice not in {"1", "2", "3"}:
        raise RuntimeError("影片編碼選項無效。")
    if codec_choice == "1":
        if "libx265" not in encoders:
            raise RuntimeError("目前 FFmpeg 沒有 libx265。請安裝含 x265 的 FFmpeg。")
        codec = "hevc10"
    elif codec_choice == "2":
        if "libx265" not in encoders:
            raise RuntimeError("目前 FFmpeg 沒有 libx265。請安裝含 x265 的 FFmpeg。")
        codec = "hevc8"
    else:
        if "libsvtav1" in encoders:
            codec = "libsvtav1"
        elif "libaom-av1" in encoders:
            codec = "libaom-av1"
        else:
            raise RuntimeError("目前 FFmpeg 沒有 libsvtav1 或 libaom-av1。")

    print("\n【HDR 色彩處理】")
    print("1. 依原片保留 HDR／SDR（推薦）：HDR 保留 HDR，SDR 保持 SDR，不擅自改變動態範圍。")
    print("2. HDR 轉換成 SDR（色調映射）：供 SDR 螢幕使用；會改變 HDR 呈現，不可還原成原始 HDR。")
    hdr_choice = input("選擇 1 或 2（預設 1）：").strip() or "1"
    if hdr_choice not in {"1", "2"}:
        raise RuntimeError("HDR 處理選項無效。")

    print("\n【最高輸出解析度】")
    print("1. 最高 1080p（推薦）：4K／1440p 自動縮小；低於 1080p 不升頻。")
    print("2. 最高 1440p：保留更多解析度細節，容量通常較大。")
    print("3. 保持原始解析度：只重新編碼，不縮小解析度。")
    res_choice = input("選擇 1、2 或 3（預設 1）：").strip() or "1"
    if res_choice not in {"1", "2", "3"}:
        raise RuntimeError("解析度選項無效。")
    max_size = {"1": (1920, 1080), "2": (2560, 1440), "3": None}[res_choice]

    print("\n【品質／CRF】")
    if codec in {"libsvtav1", "libaom-av1"}:
        print("AV1 品質值範圍 0～63，數字越小通常畫質越高、檔案越大。建議先測 28～35。")
        raw_quality = input("輸入 AV1 CRF（建議 30，範圍 20～40）：").strip() or "30"
        try:
            quality = int(raw_quality)
        except ValueError:
            raise RuntimeError("AV1 CRF 必須是整數。")
        if not 20 <= quality <= 40:
            raise RuntimeError("AV1 CRF 請輸入 20～40；不同編碼器品質值不能直接與 x265 比較。")
    else:
        print("x265 CRF 18～19：畫質優先、檔案通常較大。")
        print("x265 CRF 20～21：畫質與容量平衡（推薦從 21 測試）。")
        print("x265 CRF 22～23：容量優先，暗場、顆粒和細節損失風險較高。")
        print("x265 CRF 24～27：較激進壓縮，不建議作為收藏預設。")
        raw_quality = input("輸入 x265 CRF（預設 21，範圍 18～27）：").strip() or "21"
        try:
            quality = int(raw_quality)
        except ValueError:
            raise RuntimeError("x265 CRF 必須是整數。")
        if not 18 <= quality <= 27:
            raise RuntimeError("x265 CRF 請輸入 18～27。")

    print("\n【編碼速度 Preset】")
    if codec in {"libsvtav1", "libaom-av1"}:
        if codec == "libsvtav1":
            print("SVT-AV1 速度值與 x265 不同：數字越小通常越慢、壓縮效率越高。")
            print("1. Preset 8：較快，壓制速度優先。")
            print("2. Preset 6（推薦）：速度與壓縮效率平衡。")
            print("3. Preset 4：更慢，可能改善壓縮效率，但更耗時。")
            print("4. Preset 2：非常慢。")
            print("5. Preset 0：極慢，不建議日常使用。")
        else:
            print("libaom-av1 使用 cpu-used：數字越大越快，但壓縮效率通常較低。")
            print("1. cpu-used 6：速度較快。")
            print("2. cpu-used 4（推薦）：速度與效率平衡。")
            print("3. cpu-used 3：較慢。")
            print("4. cpu-used 2：非常慢。")
            print("5. cpu-used 1：極慢，不建議日常使用。")
    else:
        print("1. Medium：較快，壓縮效率相對低一些。")
        print("2. Slow（推薦）：速度與壓縮效率較均衡。")
        print("3. Slower：分析更多，可能再省一些容量，但更耗時。")
        print("4. Very Slow：耗時很高，額外收益通常逐漸遞減。")
        print("5. Placebo：極端耗時，通常不值得，不推薦日常使用。")
    preset_choice = input("選擇 1～5（預設 2：推薦平衡）：").strip() or "2"
    preset_map = {"1": "medium", "2": "slow", "3": "slower", "4": "veryslow", "5": "placebo"}
    if preset_choice not in preset_map:
        raise RuntimeError("Preset 選項無效。")
    x265_preset = preset_map[preset_choice]
    av1_preset_map = {"1": 8, "2": 6, "3": 4, "4": 2, "5": 0}

    print("\n【自動跳過已高效率編碼片源】")
    print("若來源已符合所選編碼及位深，且不需縮小解析度，預設跳過二次有損壓制。")
    print("若來源格式與所選輸出不同（例如 HEVC→AV1），仍會正常重新編碼。選 N 可強制重壓已符合格式的片源。")
    print("例如來源已是 HEVC 10-bit，但你確定要按 CRF 21 再壓一次，這裡必須輸入 N。")
    skip_choice = input("啟用相同格式自動跳過？輸入 Y／N（預設 Y）：").strip().lower() or "y"
    if skip_choice not in {"y", "n"}:
        raise RuntimeError("請輸入 Y 或 N。")

    unknown_color_choice = "n"
    if hdr_choice == "1" and codec != "hevc8":
        print("\n【來源色彩標籤不完整時的處理】")
        print("若來源是 10-bit／BT.2020，但缺少足夠的 transfer metadata，預設停止重新編碼。")
        print("選 Y：只在保留 HDR／SDR 模式及 10-bit 輸出時，允許不做色調映射、保留來源可讀色彩標籤後繼續編碼。")
        print("不會自行補寫 BT.709，也不會把未知來源當成 SDR；HDR→SDR 模式仍一律停止。")
        unknown_color_choice = input("允許這種「不轉換色彩、只重新編碼像素」的例外？輸入 Y／N（預設 N）：").strip().lower() or "n"
        if unknown_color_choice not in {"y", "n"}:
            raise RuntimeError("請輸入 Y 或 N。")

    return {
        "codec": codec, "hdr_mode": "preserve" if hdr_choice == "1" else "sdr",
        "max_size": max_size, "quality": quality, "x265_preset": x265_preset,
        "av1_preset": av1_preset_map[preset_choice], "auto_skip": skip_choice == "y",
        "allow_unknown_color_metadata": unknown_color_choice == "y",
        "preset_choice": preset_choice,
    }


def _fraction_to_float(value):
    try:
        if isinstance(value, str) and "/" in value:
            a, b = value.split("/", 1)
            return float(a) / float(b)
        return float(value)
    except Exception:
        return None


def build_scale_filter(max_size):
    if not max_size:
        return None
    w, h = max_size
    return "scale=w='min({},iw)':h='min({},ih)':force_original_aspect_ratio=decrease:force_divisible_by=2:flags=lanczos".format(w, h)


def run_ffmpeg_progress(cmd, logfile, duration_seconds, stderr_path, label):
    """Run FFmpeg with machine-readable progress while streaming progress to Terminal."""
    start = datetime.now()
    with open(stderr_path, "w", encoding="utf-8") as errfile:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=errfile,
                                text=True, encoding="utf-8", errors="replace", bufsize=1)
        last_line = ""
        try:
            for line in proc.stdout:
                line = line.strip()
                if line.startswith("out_time_us="):
                    try:
                        seconds = int(line.split("=", 1)[1]) / 1_000_000
                        if duration_seconds and duration_seconds > 0:
                            pct = min(100.0, seconds / duration_seconds * 100)
                            last_line = "\r{}：{:5.1f}%（{} / {:.1f} 秒）".format(label, pct, line.split("=",1)[1], duration_seconds)
                        else:
                            last_line = "\r{}：已處理 {:.1f} 秒".format(label, seconds)
                        print(last_line, end="", flush=True)
                    except Exception:
                        pass
                elif line.startswith("speed="):
                    speed = line.split("=", 1)[1]
                    if last_line:
                        print(" · speed {}".format(speed), end="", flush=True)
                elif line == "progress=end":
                    print("\r{}：FFmpeg 編碼階段結束。                    ".format(label), flush=True)
            rc = proc.wait()
        except BaseException:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
            raise
    diagnostics = Path(stderr_path).read_text(encoding="utf-8", errors="replace") if Path(stderr_path).exists() else ""
    if diagnostics.strip():
        write_block(logfile, "FFmpeg 壓制診斷輸出（{}）".format(label), diagnostics)
    write_log(logfile, "FFmpeg 編碼耗時：{} 秒；退出碼：{}。".format(
        round((datetime.now() - start).total_seconds(), 1), rc))
    if rc != 0:
        raise RuntimeError("FFmpeg 重新編碼失敗，退出碼 {}；詳見日誌。".format(rc))
    return diagnostics


def compare_hdr_metadata(source_stream, output_stream, source_side_descriptions, output_side_data=None):
    """Return a failure message if essential HDR tags/side data were lost."""
    for field in ("color_primaries", "color_transfer", "color_space"):
        src = source_stream.get(field)
        if src and src not in ("unknown", "unspecified"):
            dst = output_stream.get(field)
            if dst != src:
                return "HDR 保留驗證失敗：{} 從 {} 變成 {}。".format(field, src, dst)
    output_side = list(output_stream.get("side_data_list", []) or []) + list(output_side_data or [])
    output_desc = [str(x.get("side_data_type", "")) for x in output_side]
    important = [x for x in source_side_descriptions
                 if "mastering display metadata" in x.lower() or "content light level metadata" in x.lower()]
    for item in important:
        if not any(item.lower() in y.lower() for y in output_desc):
            return "HDR 保留驗證失敗：輸出缺少來源的 {}。".format(item)
    return None


def encode_video_and_prepare_source(ffmpeg, ffprobe, mkvmerge, mkvextract, source_path, output_root,
                                    settings, logfile, episode_label):
    """Re-encode only the primary video stream, then remux it with untouched source tracks.

    Temporary files are created under the selected output directory (not the internal system
    temp directory), so users can keep the workload on their external media disk.
    Returns (prepared_source_path, was_reencoded, message).
    """
    source_probe, probe_stderr = ffprobe_info(ffprobe, source_path)
    streams = source_probe.get("streams", [])
    all_video_streams = [x for x in streams if x.get("codec_type") == "video"]
    if any((x.get("disposition", {}) or {}).get("attached_pic", 0) for x in all_video_streams):
        raise RuntimeError("來源含 attached picture 封面影像軌；本版暫不重新編碼，以免封面影像遺失。可選無損封裝。")
    video_streams = all_video_streams
    audio_streams = [x for x in streams if x.get("codec_type") == "audio"]
    if len(video_streams) != 1:
        raise RuntimeError("為避免遺失多角度／多影片軌或封面影像，本版只處理恰好一條主要影片軌；目前辨識到 {} 條。".format(len(video_streams)))
    if not audio_streams:
        raise RuntimeError("來源沒有音訊軌，為避免輸出不完整而停止。")
    stream = video_streams[0]
    codec_name = str(stream.get("codec_name") or "unknown").lower()
    width, height = int(stream.get("width") or 0), int(stream.get("height") or 0)
    if not width or not height:
        raise RuntimeError("FFprobe 未能取得影片解析度。")
    if stream.get("field_order") not in (None, "unknown", "progressive"):
        raise RuntimeError("來源可能是隔行影片（field_order={}）。本版不會擅自去隔行，請先單獨處理或使用無損封裝。".format(stream.get("field_order")))
    pix_fmt = str(stream.get("pix_fmt") or "")
    if not (pix_fmt.startswith("yuv420") or pix_fmt.startswith("nv12") or pix_fmt.startswith("p010")):
        raise RuntimeError("來源色度取樣／像素格式為 {}，不是一般 4:2:0 片源。本版暫不自動降採樣，避免未經確認損失色彩細節。".format(pix_fmt or "未知"))
    if (stream.get("tags") or {}).get("rotate") or (stream.get("side_data_list") and any("display matrix" in str(x.get("side_data_type", "")).lower() for x in stream.get("side_data_list", []))):
        raise RuntimeError("來源含旋轉／Display Matrix 資訊；本版暫不自動轉換，避免輸出方向改變。")

    hdr_kind, source_side, source_side_descriptions = detect_hdr_kind(ffprobe, source_path, stream)
    is_hdr = hdr_kind in {"hdr10", "hlg"}

    max_size = settings.get("max_size")
    need_scale = bool(max_size and (width > max_size[0] or height > max_size[1]))
    source_bytes = source_path.stat().st_size
    source_is_10bit = "10" in pix_fmt or "p010" in pix_fmt.lower()
    target_matches_source = (
        (settings["codec"] == "hevc10" and codec_name == "hevc" and source_is_10bit) or
        (settings["codec"] == "hevc8" and codec_name == "hevc" and not source_is_10bit) or
        (settings["codec"] in {"libsvtav1", "libaom-av1"} and codec_name == "av1" and source_is_10bit)
    )
    # An unknown transfer function is only a blocker when we actually need to
    # re-encode/transform pixels. If the source already matches the selected
    # codec/bit depth and resolution, auto-skip must happen first: the original
    # video bitstream and its incomplete metadata are passed through unchanged.
    metadata_uncertain = (hdr_kind == "unknown" and
                          ("10" in pix_fmt or str(stream.get("color_primaries") or "").lower() == "bt2020"))
    needs_tonemap = settings["hdr_mode"] == "sdr" and (hdr_kind not in {"sdr_or_other"})
    if settings.get("auto_skip") and target_matches_source and not need_scale and not needs_tonemap:
        write_log(logfile, "{}：來源已符合所選編碼／位深（{}，{}），且不需降低解析度或轉換 HDR；依自動保護設定跳過二次有損壓制。色彩 metadata 即使不完整，也會隨原始影片位元流一併保留，不做色彩轉換。".format(
            episode_label, codec_name.upper(), pix_fmt))
        return source_path, False, "自動跳過二次壓制（來源已符合所選編碼及位深，不需縮小解析度；原色彩 metadata 原樣保留）"

    # When an actual encode/resize/tone-map is necessary, incomplete transfer
    # metadata cannot safely distinguish HDR from SDR. Do not guess or relabel.
    if settings["hdr_mode"] == "sdr" and hdr_kind == "unknown":
        raise RuntimeError("選擇了 HDR→SDR，但來源色彩傳遞資訊不足，無法確認是否為 HDR。請先確認並補齊色彩 metadata，或改選依原片保留。原始檔未修改。")
    if metadata_uncertain:
        allow_unknown = settings.get("allow_unknown_color_metadata", False)
        if (not allow_unknown or settings["hdr_mode"] != "preserve" or
                settings["codec"] == "hevc8"):
            raise RuntimeError("來源是 10-bit／廣色域，但缺少足夠的色彩傳遞資訊，無法可靠判斷 HDR 或 SDR。預設會停止；若你確認要保持來源色彩標籤、不做 HDR→SDR，請在壓制設定中明確允許這項 10-bit 例外。HDR→SDR 及 8-bit 輸出不允許使用此例外。原始檔未修改。")
        write_log(logfile, "警告：使用者已明確允許來源色彩 metadata 不完整時繼續 10-bit 重新編碼；不做色調映射、不補寫 BT.709，只保留來源中可讀取的原有色彩標籤。請以原播放器比較樣片。")
    if hdr_kind in {"dolby_vision", "hdr10plus"}:
        raise RuntimeError("來源識別到 {} 動態 HDR 資訊；本版不會冒險丟失動態 metadata。請對這集選擇無損封裝，保留原檔。".format(
            "Dolby Vision" if hdr_kind == "dolby_vision" else "HDR10+"))
    if is_hdr and settings["hdr_mode"] == "preserve" and settings["codec"] == "hevc8":
        raise RuntimeError("來源是 HDR，但選擇了 HEVC 8-bit。若要保留 HDR，必須使用 10-bit 編碼；請改選 HEVC Main10／AV1 10-bit，或選擇 HDR 轉 SDR。")

    free_bytes = shutil.disk_usage(output_root).free
    estimated_required = source_bytes * 2 + (512 * 1024 * 1024)
    if free_bytes < estimated_required:
        raise RuntimeError("輸出磁碟可用空間不足。開始壓制前保守估計至少需要約 {:.1f} GiB（來源檔大小的 2 倍再加 512 MiB），目前只有 {:.1f} GiB。".format(
            estimated_required / (1024 ** 3), free_bytes / (1024 ** 3)))

    # Build FFmpeg command for video-only encoding. Audio, subtitles, chapters and attachments
    # are later copied from the original source by MKVToolNix.
    with tempfile.TemporaryDirectory(prefix=".frame-v2-work-", dir=str(output_root)) as temp_dir:
        work = Path(temp_dir)
        encoded_video = work / "encoded_video_only.mkv"
        prepared_source = work / "prepared_source.mkv"
        filters = []
        if is_hdr and settings["hdr_mode"] == "sdr":
            filter_names = ffmpeg_filter_names(ffmpeg)
            if "zscale" not in filter_names or "tonemap" not in filter_names:
                raise RuntimeError("目前 FFmpeg 缺少 zscale／tonemap 濾鏡，無法安全做 HDR→SDR 色調映射。")
            filters.extend([
                "zscale=t=linear:npl=100",
                "format=gbrpf32le",
                "tonemap=tonemap=hable:desat=0",
                "zscale=p=bt709:t=bt709:m=bt709:r=tv",
            ])
        scale_filter = build_scale_filter(max_size)
        if scale_filter:
            filters.append(scale_filter)

        if settings["codec"] == "hevc10":
            encoder, pix_fmt_out, target_codec = "libx265", "yuv420p10le", "hevc"
        elif settings["codec"] == "hevc8":
            encoder, pix_fmt_out, target_codec = "libx265", "yuv420p", "hevc"
        else:
            encoder, pix_fmt_out, target_codec = settings["codec"], "yuv420p10le", "av1"
        filters.append("format={}".format(pix_fmt_out))

        cmd = [ffmpeg, "-hide_banner", "-nostdin", "-y", "-i", str(source_path),
               "-map", "0:{}".format(stream.get("index", 0)), "-an", "-sn", "-dn", "-map_metadata", "-1", "-map_chapters", "-1"]
        if filters:
            cmd += ["-vf", ",".join(filters)]
        cmd += ["-c:v", encoder]
        if settings["codec"] in {"hevc10", "hevc8"}:
            cmd += ["-preset", settings["x265_preset"], "-crf", str(settings["quality"]), "-profile:v",
                    "main10" if settings["codec"] == "hevc10" else "main"]
            if is_hdr and settings["hdr_mode"] == "preserve" and hdr_kind == "hdr10":
                cmd += ["-x265-params", "hdr10=1:repeat-headers=1"]
        else:
            cmd += ["-crf", str(settings["quality"])]
            if settings["codec"] == "libsvtav1":
                cmd += ["-preset", str(settings["av1_preset"])]
            elif settings["codec"] == "libaom-av1":
                aom_cpu = {1: 6, 2: 4, 3: 3, 4: 2, 5: 1}[int(settings["preset_choice"])]
                cmd += ["-cpu-used", str(aom_cpu), "-b:v", "0"]
        cmd += ["-pix_fmt", pix_fmt_out, "-fps_mode", "passthrough"]
        if is_hdr and settings["hdr_mode"] == "preserve":
            for ff_field, value in (("-color_primaries", stream.get("color_primaries")),
                                    ("-color_trc", stream.get("color_transfer")),
                                    ("-colorspace", stream.get("color_space")),
                                    ("-color_range", stream.get("color_range"))):
                if value and value not in {"unknown", "unspecified"}:
                    cmd += [ff_field, str(value)]
        elif is_hdr and settings["hdr_mode"] == "sdr":
            cmd += ["-color_primaries", "bt709", "-color_trc", "bt709", "-colorspace", "bt709", "-color_range", "tv"]
        else:
            for ff_field, value in (("-color_primaries", stream.get("color_primaries")),
                                    ("-color_trc", stream.get("color_transfer")),
                                    ("-colorspace", stream.get("color_space")),
                                    ("-color_range", stream.get("color_range"))):
                if value and value not in {"unknown", "unspecified"}:
                    cmd += [ff_field, str(value)]
        cmd += ["-f", "matroska", "-progress", "pipe:1", "-nostats", str(encoded_video)]

        write_log(logfile, "{}：開始影片重新編碼；來源 {}×{}、編碼 {}、色彩類型 {}、輸出上限 {}。".format(
            episode_label, width, height, settings["codec"], hdr_kind,
            "保持原始" if not max_size else "{}×{}".format(*max_size)))
        if probe_stderr:
            write_block(logfile, "原始來源 ffprobe stderr", probe_stderr)
        duration = _fraction_to_float((source_probe.get("format") or {}).get("duration"))
        stderr_path = work / "ffmpeg-encode.stderr.log"
        run_ffmpeg_progress(cmd, logfile, duration, stderr_path, episode_label)
        if not encoded_video.is_file() or encoded_video.stat().st_size == 0:
            raise RuntimeError("FFmpeg 未產生有效的影片軌暫存檔。")

        encoded_probe, encoded_probe_stderr = ffprobe_info(ffprobe, encoded_video)
        encoded_videos = [x for x in encoded_probe.get("streams", []) if x.get("codec_type") == "video"]
        if len(encoded_videos) != 1 or encoded_videos[0].get("codec_name") != target_codec:
            raise RuntimeError("重新編碼後影片軌驗證失敗：預期 {}，實際 {}。".format(
                target_codec, [x.get("codec_name") for x in encoded_videos]))
        encoded_stream = encoded_videos[0]
        if max_size and (int(encoded_stream.get("width", 0)) > max_size[0] or int(encoded_stream.get("height", 0)) > max_size[1]):
            raise RuntimeError("輸出解析度超出所選上限。")
        if is_hdr and settings["hdr_mode"] == "preserve":
            encoded_frame_side = ffprobe_first_frame_side_data(ffprobe, encoded_video)
            hdr_error = compare_hdr_metadata(stream, encoded_stream, source_side_descriptions, encoded_frame_side)
            if hdr_error:
                raise RuntimeError(hdr_error + "未建立最終 MKV；原始檔仍保持不變。")
        elif metadata_uncertain and settings.get("allow_unknown_color_metadata"):
            color_error = compare_hdr_metadata(stream, encoded_stream, [], [])
            if color_error:
                raise RuntimeError(color_error + "來源色彩標籤不完整例外的保留檢查失敗；未建立最終 MKV。")

        # Merge the encoded video track with the original file's untouched non-video streams.
        # --no-video applies to the original source only; audio/subtitles/chapters/attachments
        # and Matroska metadata are read from it without audio/subtitle transcoding.
        source_mkv_info, source_mkv_rc, source_mkv_stderr = mkvmerge_info(mkvmerge, source_path)
        source_mkv_video_tracks = [t for t in source_mkv_info.get("tracks", []) if t.get("type") == "video"]
        if len(source_mkv_video_tracks) != 1:
            raise RuntimeError("MKVToolNix 對來源主要影片軌的辨識數量不是 1，停止以避免錯誤映射。")
        original_video_props = source_mkv_video_tracks[0].get("properties", {}) or {}
        prep_cmd = [mkvmerge, "--output", str(prepared_source)]
        if original_video_props.get("language"):
            prep_cmd += ["--language", "0:{}".format(original_video_props["language"])]
        if original_video_props.get("track_name"):
            prep_cmd += ["--track-name", "0:{}".format(original_video_props["track_name"])]
        if isinstance(original_video_props.get("default_track"), bool):
            prep_cmd += ["--default-track-flag", "0:{}".format("yes" if original_video_props["default_track"] else "no")]
        prep_cmd += [str(encoded_video), "--no-video", str(source_path)]
        prep = run_text(prep_cmd)
        prep_output = "\n".join(x for x in [prep.stdout, prep.stderr] if x).strip()
        if prep_output:
            write_block(logfile, "mkvmerge 建立壓制來源（{}）完整輸出".format(episode_label), prep_output)
        write_log(logfile, "mkvmerge 暫存來源封裝退出碼：{}（0=無警告；1=成功但有警告；2=錯誤）。".format(prep.returncode))
        if prep.returncode not in (0, 1) or not prepared_source.is_file() or prepared_source.stat().st_size == 0:
            raise RuntimeError("無法將重新編碼的影片軌與原始其他軌道合併；詳見日誌。")

        prepared_probe, prepared_stderr = ffprobe_info(ffprobe, prepared_source)
        prepared_mkv, prepared_rc, prepared_mkv_stderr = mkvmerge_info(mkvmerge, prepared_source)
        original_counts = stream_counts(source_probe)
        prepared_counts = stream_counts(prepared_probe)
        for kind, label in (("audio", "音訊"), ("subtitle", "字幕")):
            if original_counts.get(kind, 0) != prepared_counts.get(kind, 0):
                raise RuntimeError("暫存來源的{}軌數量與原始檔不同（原始 {}、暫存 {}）；停止，避免遺失軌道。".format(
                    label, original_counts.get(kind, 0), prepared_counts.get(kind, 0)))
        if stream_codec_counts(prepared_probe, "audio") != stream_codec_counts(source_probe, "audio"):
            raise RuntimeError("暫存來源的音訊編碼與原始檔不同；停止，避免音訊被轉碼或遺失。")
        source_audio_tracks = [t for t in source_mkv_info.get("tracks", []) if t.get("type") == "audio"]
        prepared_audio_tracks = [t for t in prepared_mkv.get("tracks", []) if t.get("type") == "audio"]
        if len(source_audio_tracks) != len(prepared_audio_tracks):
            raise RuntimeError("暫存來源音訊軌數量與原始檔不同。")
        for idx, (src_track, out_track) in enumerate(zip(source_audio_tracks, prepared_audio_tracks), 1):
            sp = src_track.get("properties", {}) or {}
            op = out_track.get("properties", {}) or {}
            for key in ("language", "language_ietf", "track_name", "default_track", "forced_track"):
                matches, note = compare_track_property(sp, op, key)
                if not matches:
                    raise RuntimeError(
                        "暫存來源第 {} 條原音訊的 {} 資訊發生變化；停止以保護原音訊設定。"
                        "（來源={!r}；暫存={!r}）".format(idx, key, sp.get(key), op.get(key))
                    )
                if note:
                    write_log(logfile, "音訊軌 {}：{}".format(idx, note))
        # P0: strict preserved-track and container metadata comparison. The video
        # payload is intentionally replaced, so only original audio/subtitles plus
        # chapters and attachments are protected at this stage.
        preserved_ok, preserved_message, preserved_notes = compare_mkv_track_metadata(
            source_mkv_info, prepared_mkv, ("audio", "subtitles"), context="壓制準備檔 ")
        if not preserved_ok:
            raise RuntimeError(preserved_message)
        for preserved_note in preserved_notes:
            write_log(logfile, preserved_note)
        ff_preserved_ok, ff_preserved_message, ff_preserved_notes = compare_ffprobe_stream_metadata(
            source_probe, prepared_probe, ("audio", "subtitle"), context="壓制準備檔 ")
        if not ff_preserved_ok:
            raise RuntimeError(ff_preserved_message)
        for preserved_note in ff_preserved_notes:
            write_log(logfile, preserved_note)
        packet_ok, packet_message = compare_ffprobe_packet_counts(
            ffprobe, source_path, prepared_source, ("audio", "subtitle"),
            context="壓制準備檔 ", logfile=logfile)
        if not packet_ok:
            raise RuntimeError(packet_message)
        write_log(logfile, packet_message)
        chapters_ok, chapters_message = compare_chapters_and_attachments(
            source_mkv_info, prepared_mkv, context="壓制準備檔 ")
        if not chapters_ok:
            raise RuntimeError(chapters_message)
        attachment_ok, attachment_message = compare_mkv_attachment_payloads(
            mkvextract, source_path, source_mkv_info, prepared_source, prepared_mkv, logfile)
        if not attachment_ok:
            raise RuntimeError(attachment_message)
        write_log(logfile, chapters_message)
        write_log(logfile, attachment_message)

        source_attach = len(source_mkv_info.get("attachments", []) or [])
        prepared_attach = len(prepared_mkv.get("attachments", []) or [])
        if source_attach != prepared_attach:
            raise RuntimeError("暫存來源附件數量與原始檔不同（原始 {}、暫存 {}）；停止，避免丟失封面／字型附件。".format(
                source_attach, prepared_attach))
        if "chapters" in source_mkv_info and "chapters" in prepared_mkv:
            if len(source_mkv_info.get("chapters", []) or []) != len(prepared_mkv.get("chapters", []) or []):
                raise RuntimeError("暫存來源章節數量與原始檔不同；停止以保護章節資訊。")
        if source_mkv_stderr:
            write_block(logfile, "原始來源 mkvmerge -J stderr", source_mkv_stderr)
        if source_mkv_rc == 1:
            write_log(logfile, "注意：原始來源 mkvmerge -J 回傳警告代碼 1；診斷輸出已保存。")
        src_subs = [t for t in source_mkv_info.get("tracks", []) if t.get("type") == "subtitles"]
        prep_subs = [t for t in prepared_mkv.get("tracks", []) if t.get("type") == "subtitles"]
        if len(src_subs) != len(prep_subs):
            raise RuntimeError("暫存來源的字幕軌數量與原始檔不同；停止以保護字幕。")
        for idx, (src_track, out_track) in enumerate(zip(src_subs, prep_subs), 1):
            sp = src_track.get("properties", {}) or {}
            op = out_track.get("properties", {}) or {}
            for key in ("language", "language_ietf", "track_name", "default_track", "forced_track"):
                matches, note = compare_track_property(sp, op, key)
                if not matches:
                    raise RuntimeError(
                        "暫存來源第 {} 條原字幕的 {} 資訊發生變化；停止以保護原字幕設定。"
                        "（來源={!r}；暫存={!r}）".format(idx, key, sp.get(key), op.get(key))
                    )
                if note:
                    write_log(logfile, "原字幕軌 {}：{}".format(idx, note))
        prepared_videos = [x for x in prepared_probe.get("streams", []) if x.get("codec_type") == "video"]
        if len(prepared_videos) != 1 or prepared_videos[0].get("codec_name") != target_codec:
            raise RuntimeError("暫存來源影片軌驗證失敗。")
        prepared_video_tracks = [t for t in prepared_mkv.get("tracks", []) if t.get("type") == "video"]
        if len(prepared_video_tracks) != 1:
            raise RuntimeError("暫存來源 MKVToolNix 影片軌數量錯誤。")
        prepared_video_props = prepared_video_tracks[0].get("properties", {}) or {}
        for key in ("language", "track_name", "default_track"):
            if original_video_props.get(key) != prepared_video_props.get(key):
                raise RuntimeError("壓制後原影片軌的 {} 資訊沒有保留。".format(key))
        if prepared_rc == 1:
            write_log(logfile, "注意：暫存來源 mkvmerge -J 有警告；原始診斷輸出已保存。")
        if prepared_stderr:
            write_block(logfile, "暫存來源 FFprobe stderr", prepared_stderr)
        if prepared_mkv_stderr:
            write_block(logfile, "暫存來源 mkvmerge -J stderr", prepared_mkv_stderr)

        # The encoded-only temporary is no longer needed; free its space before final muxing.
        try:
            encoded_video.unlink()
        except OSError:
            pass
        # Before writing the final MKV, make sure there is room for another file roughly
        # the size of the prepared source. This second check accounts for CRF output being
        # larger than the original source in some edge cases.
        prepared_bytes = prepared_source.stat().st_size
        free_before_final = shutil.disk_usage(output_root).free
        final_required = prepared_bytes + (512 * 1024 * 1024)
        if free_before_final < final_required:
            raise RuntimeError("壓制後暫存檔已建立，但輸出磁碟剩餘空間不足以安全建立最終 MKV；至少還需要約 {:.1f} GiB，目前 {:.1f} GiB。暫存檔會自動清理，原始檔不受影響。".format(
                final_required / (1024 ** 3), free_before_final / (1024 ** 3)))
        # Move the prepared source out of TemporaryDirectory only for the caller's mux call.
        # The caller must invoke its mux before this function's context is exited, so instead
        # materialize a caller-managed temp directory path through a context helper below.
        durable_prepared = output_root / (".frame-v2-prepared-{}-{}.mkv".format(os.getpid(), re.sub(r"[^A-Za-z0-9_-]", "", episode_label)))
        if durable_prepared.exists():
            raise RuntimeError("暫存檔名衝突，已停止，不會覆蓋任何檔案。")
        shutil.move(str(prepared_source), str(durable_prepared))
        write_log(logfile, "壓制來源準備完成：影片已重新編碼；原始音訊、字幕、附件及章節以 MKVToolNix 複製。")
        return durable_prepared, True, "已重新編碼為 {}；其餘軌道原樣複製".format(target_codec.upper())


def cleanup_prepared_source(path):
    if path and Path(path).name.startswith(".frame-v2-prepared-"):
        try:
            Path(path).unlink()
        except OSError:
            pass


def mux_two_external_subtitles(mkvmerge, mkvextract, ffmpeg, ffprobe, video, primary_sub, second_sub,
                               output, logfile, second_track_name="Chinese + English",
                               second_language="und", require_source_without_subtitles=False,
                               primary_language="eng", primary_track_name="English"):
    """Mux selected primary-language subtitle + a secondary subtitle."""
    output.parent.mkdir(parents=True, exist_ok=True)
    if second_language == "und" or normalize_ietf_language_tag(second_language).startswith("zh-"):
        second_label = "中文＋原語言雙語字幕"
    elif second_language == "eng" and "sdh" in second_track_name.casefold():
        second_label = "SDH 英文字幕"
    else:
        second_label = "純中文字幕"
    try:
        primary_label = {"eng": "純英文字幕", "jpn": "日文字幕", "kor": "韓文字幕"}.get(
            primary_language, "預設語言字幕")
        for label, subtitle_path in ((primary_label, primary_sub), (second_label, second_sub)):
            sub_info, packet_count, sub_stderr = validate_subtitle_source(ffprobe, subtitle_path)
            write_log(logfile, "{}來源檢查：{}，{} 個字幕事件。".format(
                label, sub_info.get("codec_name", "未知格式"), packet_count))
            if sub_stderr:
                write_block(logfile, "{} ffprobe stderr".format(label), sub_stderr)
        source_probe, source_mkv, source_probe_stderr, source_mkv_stderr, source_mkv_rc = validate_source(
            ffprobe, mkvmerge, video
        )
        source_probe["_mkvmerge_tracks"] = source_mkv.get("tracks", [])
        source_probe["_mkvmerge_info"] = source_mkv
        source_probe["_source_path"] = str(video)
        source_probe["_source_mkvmerge_warning"] = (source_mkv_rc == 1)
        source_subtitle_count = mkv_track_counts(source_mkv).get("subtitles", 0)
        if require_source_without_subtitles and source_subtitle_count:
            return False, (
                "來源影片已有 {} 條內嵌字幕；本雙外掛字幕模式不會丟棄來源字幕，"
                "因此停止本集。請改用保留來源字幕的封裝模式，或先確認並另行整理來源字幕。"
            ).format(source_subtitle_count)
        if source_probe_stderr:
            write_block(logfile, "來源 ffprobe stderr", source_probe_stderr)
        if source_mkv_stderr:
            write_block(logfile, "來源 mkvmerge -J stderr", source_mkv_stderr)
        write_log(logfile, "來源檢查：影片 {} 軌、音訊 {} 軌、內嵌字幕 {} 條。".format(
            stream_counts(source_probe).get("video", 0), stream_counts(source_probe).get("audio", 0),
            source_subtitle_count))
        if source_mkv_rc == 1:
            write_log(logfile, "注意：來源 mkvmerge -J 回傳警告代碼 1；仍已成功取得軌道資訊。")
        audio_plan = build_audio_language_plan(source_mkv, primary_language)
        log_audio_language_plan(logfile, audio_plan)
        source_probe, source_mkv = filtered_source_views(source_probe, source_mkv, audio_plan)
    except Exception as exc:
        return False, "來源檢查失敗：{}".format(exc)

    cmd = [
        mkvmerge, "--output", str(output),
    ] + audio_language_cli_options(audio_plan) + [
        # This workflow is only used for a source with no embedded subtitle tracks.
        "--no-subtitles", str(video),
        "--language", "0:{}".format(primary_language),
        "--track-name", "0:{}".format(primary_track_name),
        "--default-track-flag", "0:yes",
        "--forced-display-flag", "0:no",
        str(primary_sub),
        "--language", "0:{}".format(second_language),
        "--track-name", "0:{}".format(second_track_name),
        "--default-track-flag", "0:no",
        "--forced-display-flag", "0:no",
        str(second_sub),
    ]
    try:
        proc = run_text(cmd)
    except Exception as exc:
        return False, "無法啟動 mkvmerge：{}".format(exc)

    mux_output = "\n".join(x for x in [proc.stdout, proc.stderr] if x).strip()
    if mux_output:
        write_block(logfile, "mkvmerge 封裝完整輸出（{}）".format(output.name), mux_output)
    if proc.returncode not in (0, 1):
        if output.exists():
            try:
                output.unlink()
            except OSError:
                pass
        return False, "mkvmerge 封裝失敗，退出碼 {}；詳見日誌。".format(proc.returncode)
    if not output.exists() or output.stat().st_size == 0:
        return False, "沒有產生有效的輸出檔。"

    ok, message, warnings = verify_output(
        mkvmerge, ffmpeg, ffprobe, output, source_probe, logfile,
        second_track_name=second_track_name, second_language=second_language, mkvextract=mkvextract,
        primary_track_name=primary_track_name, primary_language=primary_language)
    if proc.returncode == 1:
        if mux_output:
            write_log(logfile, "mkvmerge 退出碼：1（成功但有警告）")
        else:
            write_log(logfile, "mkvmerge 退出碼：1（成功但有警告；工具未提供文字內容）")
        warnings.append("mkvmerge 退出碼 1：成功但有警告，完整輸出已保存")
    else:
        write_log(logfile, "mkvmerge 退出碼：0（無警告）")
    if not ok:
        return False, message + "；未驗證檔會移至 Failed 供診斷。"
    if warnings:
        return True, message + "（有警告，詳見日誌）"
    return True, message


def mux_one(mkvmerge, mkvextract, ffmpeg, ffprobe, video, primary_sub, bilingual_sub, output, logfile,
            primary_language="eng", primary_track_name="English"):
    """Mux primary EN/JA/KO plus a locale-tagged Chinese+original-language backup."""
    bilingual = identify_bilingual_subtitle_profile(bilingual_sub, primary_language)
    write_log(logfile, "雙語字幕語系識別：{}；LanguageIETF={}；軌道名稱={!r}；{}。".format(
        bilingual_sub, bilingual["language_ietf"], bilingual["track_name"], bilingual["source_note"]))
    return mux_two_external_subtitles(
        mkvmerge, mkvextract, ffmpeg, ffprobe, video, primary_sub, bilingual_sub, output, logfile,
        second_track_name=bilingual["track_name"], second_language=bilingual["language_ietf"],
        require_source_without_subtitles=False, primary_language=primary_language,
        primary_track_name=primary_track_name)


def mux_english_chinese_only(mkvmerge, mkvextract, ffmpeg, ffprobe, video, primary_sub, chinese_sub,
                             output, logfile, primary_language="eng", primary_track_name="English"):
    """No-embedded-subtitle mode: selected primary language default + Chinese backup."""
    return mux_two_external_subtitles(
        mkvmerge, mkvextract, ffmpeg, ffprobe, video, primary_sub, chinese_sub, output, logfile,
        second_track_name="Chinese", second_language="zh",
        require_source_without_subtitles=True, primary_language=primary_language,
        primary_track_name=primary_track_name)


def mux_rarbg_english_sdh(mkvmerge, mkvextract, ffmpeg, ffprobe, video, english_sub, sdh_sub, output, logfile):
    """Mux RARBG standard English + English SDH; require a source without embedded subtitles."""
    return mux_two_external_subtitles(
        mkvmerge, mkvextract, ffmpeg, ffprobe, video, english_sub, sdh_sub, output, logfile,
        second_track_name="English SDH", second_language="eng",
        require_source_without_subtitles=True, primary_language="eng", primary_track_name="English")


def parse_timestamp_seconds(value):
    """Parse HH:MM:SS or MM:SS into seconds; reject ambiguous input."""
    parts = value.strip().split(":")
    if len(parts) not in (2, 3) or any(not part.isdigit() for part in parts):
        raise ValueError("時間格式請使用 HH:MM:SS 或 MM:SS。")
    nums = [int(x) for x in parts]
    if len(nums) == 2:
        minutes, seconds = nums
        if seconds >= 60:
            raise ValueError("秒數必須小於 60。")
        return minutes * 60 + seconds
    hours, minutes, seconds = nums
    if minutes >= 60 or seconds >= 60:
        raise ValueError("分鐘與秒數都必須小於 60。")
    return hours * 3600 + minutes * 60 + seconds


def format_timestamp_seconds(value):
    value = max(0, int(round(value)))
    hours, remain = divmod(value, 3600)
    minutes, seconds = divmod(remain, 60)
    return "{:02d}:{:02d}:{:02d}".format(hours, minutes, seconds)


def count_remuxed_video_bytes(ffmpeg, source_path, stream_index, logfile):
    """Count a stream-copy-only Matroska remux via stdout; write no media file."""
    stderr_path = logfile.parent / ".quality-test-size-probe.stderr.log"
    cmd = [ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error", "-i", str(source_path),
           "-map", "0:{}".format(stream_index), "-an", "-sn", "-dn",
           "-map_metadata", "-1", "-map_chapters", "-1", "-c:v", "copy",
           "-f", "matroska", "pipe:1"]
    total = 0
    with stderr_path.open("w", encoding="utf-8") as errfile:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=errfile)
        try:
            while True:
                block = proc.stdout.read(1024 * 1024)
                if not block:
                    break
                total += len(block)
            rc = proc.wait()
        except BaseException:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
            raise
    diagnostics = stderr_path.read_text(encoding="utf-8", errors="replace") if stderr_path.exists() else ""
    try:
        stderr_path.unlink()
    except OSError:
        pass
    if diagnostics.strip():
        write_block(logfile, "原始影片軌大小統計 FFmpeg 診斷", diagnostics)
    if rc != 0 or total <= 0:
        raise RuntimeError("無法透過串流複製統計原始影片軌大小（退出碼 {}）。".format(rc))
    return total


def validate_quality_sample(ffmpeg, ffprobe, sample_path, max_size, expected_duration, logfile):
    probe, probe_stderr = ffprobe_info(ffprobe, sample_path)
    videos = [x for x in probe.get("streams", []) if x.get("codec_type") == "video"]
    audios = [x for x in probe.get("streams", []) if x.get("codec_type") == "audio"]
    if len(videos) != 1 or videos[0].get("codec_name") != "hevc":
        raise RuntimeError("樣片影片軌驗證失敗：預期恰好一條 HEVC 影片軌。")
    stream = videos[0]
    if stream.get("pix_fmt") != "yuv420p10le":
        raise RuntimeError("樣片位深驗證失敗：預期 yuv420p10le，實際為 {}。".format(stream.get("pix_fmt")))
    if audios:
        raise RuntimeError("品質樣片應只含影片軌，但輸出意外包含音訊。")
    if max_size and (int(stream.get("width", 0)) > max_size[0] or int(stream.get("height", 0)) > max_size[1]):
        raise RuntimeError("樣片解析度超出 1080p 上限。")
    actual_duration = _fraction_to_float((probe.get("format") or {}).get("duration"))
    if actual_duration and abs(actual_duration - expected_duration) > 2.0:
        raise RuntimeError("樣片長度異常：預期約 {} 秒，實際 {:.2f} 秒。".format(expected_duration, actual_duration))
    decode_cmd = [ffmpeg, "-hide_banner", "-nostdin", "-v", "warning", "-xerror",
                  "-i", str(sample_path), "-map", "0:v:0", "-f", "null", "-"]
    dec = subprocess.run(decode_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                          text=True, encoding="utf-8", errors="replace")
    if dec.returncode != 0:
        write_block(logfile, "品質樣片完整解碼錯誤", dec.stderr)
        raise RuntimeError("樣片完整影片解碼未通過。")
    if probe_stderr:
        write_block(logfile, "品質樣片 FFprobe stderr", probe_stderr)
    return actual_duration or expected_duration, stream


def run_quality_test(ffmpeg, ffprobe):
    """Encode three short, video-only HEVC Main10 samples for a single MKV."""
    print("\n" + "=" * 72)
    print("單集編碼品質測試")
    print("固定測試設定：HEVC Main10 / x265 / CRF 21 / Very Slow / 最高 1080p / 保留來源色彩標籤")
    print("本模式會強制重新編碼三段樣片，不受正式批次的相同格式自動跳過影響。")
    print("樣片只含影片、不含音訊和字幕；原始檔不會修改。")
    print("=" * 72)
    try:
        source = choose_file("選擇要測試的單集 MKV 原始影片")
        if source.suffix.lower() != ".mkv":
            raise RuntimeError("品質測試模式目前只接受 MKV 檔案。")
        source_probe, probe_stderr = ffprobe_info(ffprobe, source)
        streams = source_probe.get("streams", [])
        video_streams = [x for x in streams if x.get("codec_type") == "video"]
        if len(video_streams) != 1:
            raise RuntimeError("品質測試只接受恰好一條主要影片軌；目前辨識到 {} 條。".format(len(video_streams)))
        stream = video_streams[0]
        if (stream.get("disposition", {}) or {}).get("attached_pic", 0):
            raise RuntimeError("來源影片軌被標記為封面影像，停止測試。")
        if stream.get("field_order") not in (None, "unknown", "progressive"):
            raise RuntimeError("來源可能是隔行影片（field_order={}），停止自動試壓。".format(stream.get("field_order")))
        pix_fmt = str(stream.get("pix_fmt") or "")
        if not (pix_fmt.startswith("yuv420") or pix_fmt.startswith("nv12") or pix_fmt.startswith("p010")):
            raise RuntimeError("來源像素格式 {} 不是一般 4:2:0，為避免未確認的色度損失而停止。".format(pix_fmt or "未知"))
        if (stream.get("tags") or {}).get("rotate") or any(
            "display matrix" in str(x.get("side_data_type", "")).lower()
            for x in (stream.get("side_data_list") or [])
        ):
            raise RuntimeError("來源含旋轉／Display Matrix 資訊，本版不自動轉換方向。")
        width, height = int(stream.get("width") or 0), int(stream.get("height") or 0)
        duration = _fraction_to_float((source_probe.get("format") or {}).get("duration"))
        if not duration:
            raise RuntimeError("FFprobe 未能取得有效影片片長。")
        if not width or not height:
            raise RuntimeError("FFprobe 未能取得來源解析度。")
        hdr_kind, source_side, source_side_desc = detect_hdr_kind(ffprobe, source, stream)
        if hdr_kind in {"dolby_vision", "hdr10plus"}:
            raise RuntimeError("來源識別到 Dolby Vision／HDR10+ 動態 HDR。本版不對此類片源試壓，以免測試樣片遺失動態 HDR 資訊。")
        max_size = (1920, 1080)
        source_is_10bit = "10" in pix_fmt or "p010" in pix_fmt.lower()
        metadata_uncertain = (hdr_kind == "unknown" and
                              (source_is_10bit or str(stream.get("color_primaries") or "").lower() == "bt2020"))

        print("\n來源檔：{}".format(source))
        print("片長：{}；解析度：{}×{}；編碼：{}；像素格式：{}。".format(
            format_timestamp_seconds(duration), width, height, stream.get("codec_name", "未知"), pix_fmt or "未知"))
        print("色彩資訊：primaries={}；transfer={}；matrix={}；偵測類型={}。".format(
            stream.get("color_primaries", "缺失"), stream.get("color_transfer", "缺失"),
            stream.get("color_space", "缺失"), hdr_kind))
        print("音訊軌：{} 條；字幕軌：{} 條。".format(
            sum(1 for x in streams if x.get("codec_type") == "audio"),
            sum(1 for x in streams if x.get("codec_type") == "subtitle")))
        if metadata_uncertain:
            print("注意：來源是 10-bit／廣色域，但 HDR／SDR 標籤不完整。測試只保留可讀到的原始標籤，不做 HDR→SDR、不新增 BT.709 標籤；樣片顯示效果應與原片在相同播放器的解讀方式比較。")
            color_confirm = input("是否允許只做 10-bit 重新編碼、保留已有色彩標籤（Y／N，預設 N）？").strip().lower() or "n"
            if color_confirm != "y":
                raise RuntimeError("你未確認色彩標籤不完整時的 10-bit 試壓例外，已停止。")
        if probe_stderr:
            # Keep the normal summary readable; persist details into the log below.
            pass

        crf_raw = input("x265 CRF（預設 21；18～27）：").strip() or "21"
        try:
            crf = int(crf_raw)
        except ValueError:
            raise RuntimeError("CRF 必須是整數。")
        if not 18 <= crf <= 27:
            raise RuntimeError("CRF 請輸入 18～27。")
        sample_seconds_raw = input("每段樣片長度秒數（預設 60；建議 60）：").strip() or "60"
        try:
            sample_seconds = int(sample_seconds_raw)
        except ValueError:
            raise RuntimeError("樣片秒數必須是整數。")
        if not 30 <= sample_seconds <= 120:
            raise RuntimeError("每段樣片長度需在 30～120 秒之間。")
        if duration < sample_seconds * 3:
            raise RuntimeError("影片長度不足以安排三段不重疊樣片；請縮短每段秒數。")

        labels = [
            ("一般／對話場景", 0.15),
            ("動作／快速變化場景", 0.50),
            ("暗場／複雜細節場景", 0.82),
        ]
        starts = []
        print("\n請選取三段不同類型畫面的起始時間。預設時間只是均勻分布的參考點，請盡量挑到標示的場景類型。")
        for label, fraction in labels:
            default_start = min(duration - sample_seconds, max(0, duration * fraction))
            default_text = format_timestamp_seconds(default_start)
            raw = input("{} 起點（HH:MM:SS，預設 {}）：".format(label, default_text)).strip()
            try:
                start = parse_timestamp_seconds(raw) if raw else int(default_start)
            except ValueError as exc:
                raise RuntimeError(str(exc))
            if start < 0 or start + sample_seconds > duration:
                raise RuntimeError("{} 的起點超出影片範圍，需保留完整 {} 秒。".format(label, sample_seconds))
            if any(not (start + sample_seconds <= old or old + sample_seconds <= start) for old in starts):
                raise RuntimeError("三段測試片段不能重疊，請重新執行並輸入不重疊的起點。")
            starts.append(start)

        output_root = choose_folder("選擇品質測試輸出位置（建議選外接硬碟）")
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        test_dir = output_root / "{}_QualityTest_{}".format(source.stem, stamp)
        test_dir.mkdir(parents=True, exist_ok=False)
        logfile = test_dir / "FRAME_Quality_Test_{}.log".format(stamp)
        with logfile.open("w", encoding="utf-8") as f:
            f.write("FRAME Cinema MKV Quality Test v{}\nStarted: {}\nSource: {}\n".format(
                VERSION, datetime.now().isoformat(timespec="seconds"), source))
        write_log(logfile, "測試設定：HEVC Main10 / libx265 / CRF {} / Very Slow / 最高 1080p / HDR 不轉換。".format(crf))
        write_log(logfile, "來源：{}×{}；{}；像素格式 {}；色彩類型 {}。".format(width, height, stream.get("codec_name"), pix_fmt, hdr_kind))
        if probe_stderr:
            write_block(logfile, "來源 FFprobe stderr", probe_stderr)
        if metadata_uncertain:
            write_log(logfile, "警告：使用者已確認來源 HDR／SDR metadata 不完整時進行 10-bit 試壓；只保留原本可讀到的標籤，不注入新的色彩標籤、不做 tone mapping。")

        filter_parts = []
        scale_filter = build_scale_filter(max_size)
        if scale_filter:
            filter_parts.append(scale_filter)
        filter_parts.append("format=yuv420p10le")
        if hdr_kind == "hdr10":
            hdr10_params = "hdr10=1:repeat-headers=1"
        else:
            hdr10_params = None
        sample_actual_durations = []
        for index, (label, _) in enumerate(labels, 1):
            start = starts[index - 1]
            out = test_dir / "{}_sample{}_{}_CRF{}_veryslow.mkv".format(
                source.stem, index, ["dialogue", "motion", "dark_detail"][index - 1], crf)
            if out.exists():
                raise RuntimeError("測試輸出已存在，為避免覆蓋而停止：{}".format(out))
            cmd = [ffmpeg, "-hide_banner", "-nostdin", "-y", "-ss", "{:.3f}".format(start),
                   "-i", str(source), "-t", str(sample_seconds),
                   "-map", "0:{}".format(stream.get("index", 0)), "-an", "-sn", "-dn",
                   "-map_metadata", "-1", "-map_chapters", "-1", "-vf", ",".join(filter_parts),
                   "-c:v", "libx265", "-preset", "veryslow", "-crf", str(crf),
                   "-profile:v", "main10", "-pix_fmt", "yuv420p10le", "-fps_mode", "passthrough"]
            if hdr10_params:
                cmd += ["-x265-params", hdr10_params]
            for ff_field, value in (("-color_primaries", stream.get("color_primaries")),
                                    ("-color_trc", stream.get("color_transfer")),
                                    ("-colorspace", stream.get("color_space")),
                                    ("-color_range", stream.get("color_range"))):
                if value and value not in {"unknown", "unspecified"}:
                    cmd += [ff_field, str(value)]
            cmd += ["-f", "matroska", "-progress", "pipe:1", "-nostats", str(out)]
            write_log(logfile, "\n[樣片 {}/3] {}；起點 {}；長度 {} 秒；輸出 {}。".format(
                index, label, format_timestamp_seconds(start), sample_seconds, out))
            stderr_path = test_dir / ".ffmpeg-sample-{}.stderr.log".format(index)
            try:
                run_ffmpeg_progress(cmd, logfile, sample_seconds, stderr_path, "樣片 {}/3".format(index))
            except BaseException:
                try:
                    out.unlink()
                except OSError:
                    pass
                raise
            if not out.is_file() or out.stat().st_size <= 0:
                raise RuntimeError("FFmpeg 未產生有效樣片：{}。".format(label))
            try:
                stderr_path.unlink()
            except OSError:
                pass
            actual_duration, out_stream = validate_quality_sample(
                ffmpeg, ffprobe, out, max_size, sample_seconds, logfile)
            sample_actual_durations.append(actual_duration)
            if hdr_kind in {"hdr10", "hlg"}:
                sample_side = ffprobe_first_frame_side_data(ffprobe, out)
                hdr_error = compare_hdr_metadata(stream, out_stream, source_side_desc, sample_side)
                if hdr_error:
                    raise RuntimeError(hdr_error + "品質樣片未通過 HDR metadata 保留檢查。")
            elif metadata_uncertain:
                color_error = compare_hdr_metadata(stream, out_stream, [], [])
                if color_error:
                    raise RuntimeError(color_error + "品質樣片未保留可讀取的色彩標籤。")
            write_log(logfile, "樣片驗證通過：{}×{}；{}；{}；大小 {:.2f} MB。".format(
                out_stream.get("width"), out_stream.get("height"), out_stream.get("codec_name"),
                out_stream.get("pix_fmt"), out.stat().st_size / 1_000_000))

        sample_files = sorted(test_dir.glob("*_sample*_CRF*_veryslow.mkv"))
        sample_bytes = sum(x.stat().st_size for x in sample_files)
        sample_duration_total = sum(sample_actual_durations)
        avg_encoded_bytes_per_second = sample_bytes / sample_duration_total
        avg_video_bitrate_mbps = avg_encoded_bytes_per_second * 8 / 1_000_000
        write_log(logfile, "\n三段樣片總大小：{:.2f} MB；總長度：{} 秒；平均輸出位元率（含樣片 MKV 少量封裝開銷）：{:.3f} Mbps。".format(
            sample_bytes / 1_000_000, sample_duration_total, avg_video_bitrate_mbps))

        write_log(logfile, "正在以串流複製方式統計原片影片軌大小；這會完整讀取原影片，但不會寫出影片檔或修改來源。")
        print("\n正在讀取原片並統計影片軌資料量（不產生影片檔、不修改原片）……", flush=True)
        video_only_bytes = count_remuxed_video_bytes(ffmpeg, source, stream.get("index", 0), logfile)
        retained_nonvideo_bytes = max(0, source.stat().st_size - video_only_bytes)
        estimated_encoded_video_bytes = avg_encoded_bytes_per_second * duration
        estimated_final_bytes = estimated_encoded_video_bytes + retained_nonvideo_bytes
        # A deliberately broad ±20% envelope reflects scene-to-scene bitrate variance.
        low_bytes = estimated_encoded_video_bytes * 0.80 + retained_nonvideo_bytes
        high_bytes = estimated_encoded_video_bytes * 1.20 + retained_nonvideo_bytes
        def gb(value):
            return value / 1_000_000_000
        def mb(value):
            return value / 1_000_000
        source_bytes = source.stat().st_size
        reduction = (1 - estimated_final_bytes / source_bytes) * 100 if source_bytes else 0
        summary = [
            "\n" + "=" * 72,
            "品質測試完成（所有樣片均通過完整影片解碼）",
            "設定：HEVC Main10 / x265 CRF {} / Very Slow / 最高 1080p。".format(crf),
            "原片大小：{:.3f} GB；統計到的原始影片軌封裝大小：約 {:.3f} GB。".format(gb(source_bytes), gb(video_only_bytes)),
            "三段樣片合計：{:.2f} MB / {} 秒；平均輸出位元率約 {:.3f} Mbps。".format(
                mb(sample_bytes), sample_duration_total, avg_video_bitrate_mbps),
            "預估壓制後整集大小：約 {:.3f} GB（粗估範圍 {:.3f}～{:.3f} GB）。".format(
                gb(estimated_final_bytes), gb(low_bytes), gb(high_bytes)),
            "相對原片預估減少：約 {:.1f}%。".format(reduction),
            "估算方式：三段樣片平均位元率 × 全片長度，再加回原始音訊／字幕／其他非影片資料量。",
            "注意：這是估算，不是保證；取樣場景若偏簡單或偏複雜，整集結果會有差異。",
            "測試樣片不含音訊及字幕，只用於比較畫質；原始檔及原始字幕／音訊均未修改。",
            "輸出資料夾：{}".format(test_dir),
            "詳細日誌：{}".format(logfile),
            "=" * 72,
        ]
        for line in summary:
            write_log(logfile, line, also_print=False)
        print("\n".join(summary), flush=True)
        input("\n按 Enter 結束……")
        return 0
    except Exception as exc:
        print("\n品質測試未完成：{}".format(exc))
        try:
            if "logfile" in locals() and logfile and Path(logfile).is_file():
                print("測試日誌：{}".format(logfile))
        except Exception:
            pass
        input("按 Enter 結束……")
        return 1


def main():
    print("=" * 72)
    print("FRAME Cinema｜批量 MKV 封裝與壓制工具 v{}".format(VERSION))
    print("可選無損封裝或影片重新編碼；保留原始音訊／字幕；輸出前逐集完整驗證")
    print("=" * 72)

    mkvmerge = find_mkvmerge()
    mkvextract = find_mkvextract(mkvmerge)
    ffmpeg = find_ffmpeg()
    ffprobe = find_ffprobe()
    missing = []
    if not ffmpeg:
        missing.append("ffmpeg")
    if not ffprobe:
        missing.append("ffprobe")
    if missing:
        print("\n缺少必要工具：" + "、".join(missing))
        print("請在終端機執行：brew install ffmpeg")
        input("\n按 Enter 結束……")
        return 2

    print("\n工具版本：")
    for name, path in (("ffmpeg", ffmpeg), ("ffprobe", ffprobe)):
        proc = run_text([path, "-version"])
        print("{}：{}".format(name, (proc.stdout or proc.stderr).splitlines()[0] if (proc.stdout or proc.stderr) else path))

    print("\n請選擇工作模式：")
    print("1. 批次無損封裝（影片和音訊不重新編碼）")
    print("2. 批次重新編碼影片後封裝（音訊與字幕仍直接複製）")
    print("3. 單集編碼品質測試（只產生三段短樣片，強制試壓，不會自動跳過）")
    process_choice = input("輸入 1、2 或 3（預設 1）：").strip() or "1"
    if process_choice not in {"1", "2", "3"}:
        print("\n未選擇有效工作模式，已安全退出。")
        input("按 Enter 結束……")
        return 1
    if process_choice == "3":
        return run_quality_test(ffmpeg, ffprobe)

    missing = []
    if not mkvmerge:
        missing.append("mkvmerge（MKVToolNix）")
    if not mkvextract:
        missing.append("mkvextract（MKVToolNix）")
    if missing:
        print("\n批次封裝需要：" + "、".join(missing))
        print("請在終端機執行：brew install mkvtoolnix ffmpeg")
        input("\n按 Enter 結束……")
        return 2
    for name, path in (("mkvmerge", mkvmerge), ("mkvextract", mkvextract)):
        proc = run_text([path, "--version"])
        print("{}：{}".format(name, (proc.stdout or proc.stderr).splitlines()[0] if (proc.stdout or proc.stderr) else path))

    transcode_settings = None
    if process_choice == "2":
        try:
            transcode_settings = choose_encoding_settings(ffmpeg)
        except RuntimeError as exc:
            print("\n設定無法使用：{}".format(exc))
            input("按 Enter 結束……")
            return 1
        print("\n壓制設定摘要：")
        codec_labels = {"hevc10": "HEVC Main10（10-bit）", "hevc8": "HEVC 8-bit", "libsvtav1": "AV1 10-bit（SVT-AV1）", "libaom-av1": "AV1 10-bit（libaom）"}
        print("編碼：{}；品質值：{}；最高解析度：{}；HDR：{}。".format(
            codec_labels.get(transcode_settings["codec"], transcode_settings["codec"]), transcode_settings["quality"],
            "保持原始" if not transcode_settings["max_size"] else "{}×{}".format(*transcode_settings["max_size"]),
            "依原片保留" if transcode_settings["hdr_mode"] == "preserve" else "HDR 色調映射為 SDR"))
        if transcode_settings["codec"] == "libsvtav1":
            print("SVT-AV1 Preset：{}（數字越小越慢；與 x265 preset 不可直接等同）。".format(transcode_settings["av1_preset"]))
        elif transcode_settings["codec"] == "libaom-av1":
            aom_cpu = {"1": 6, "2": 4, "3": 3, "4": 2, "5": 1}[transcode_settings["preset_choice"]]
            print("libaom-av1 cpu-used：{}（數字越大越快；與 x265 preset 不可直接等同）。".format(aom_cpu))
        else:
            print("x265 Preset：{}。".format(transcode_settings["x265_preset"]))
        print("色彩標籤不完整例外：{}。".format(
            "已允許（只保留可讀標籤、不做 tone mapping；僅 10-bit 輸出有效）"
            if transcode_settings.get("allow_unknown_color_metadata") else "未允許（保守停止）"))
        print("注意：壓制會重新編碼影片，有損；原始影片不會被修改或覆蓋。暫存工作檔會放在你選定的輸出磁碟。")

    print("\n請選擇這批作品的主要字幕語言：")
    print("1. English（美劇／英語作品；預設英文字幕，輸出 .en 字幕）")
    print("2. Japanese（日劇／日語作品；預設日文字幕，輸出 .ja 字幕）")
    print("3. Korean（韓劇／韓語作品；預設韓文字幕，輸出 .ko 字幕）")
    profile_choice = input("輸入 1、2 或 3（預設 1）：").strip() or "1"
    if profile_choice not in LANGUAGE_PROFILES:
        print("\n未選擇有效語言設定，已安全退出。")
        input("按 Enter 結束……")
        return 1
    primary_profile = LANGUAGE_PROFILES[profile_choice]
    primary_language = primary_profile["code"]
    primary_name = primary_profile["name"]
    print("\n語言設定：主要字幕 {}（{}）；預設旁掛副檔名 {}。".format(
        primary_name, primary_language, primary_profile["suffix"]))
    print("音訊保留規則：所有模式只保留 English／Japanese／Korean（eng／jpn／kor）；其他語言音軌會排除。")
    print("安全規則：音訊語言標籤缺失或互相矛盾時，該集停止，不猜測、不冒險刪除。")

    print("\n請選擇封裝模式：")
    print("1. 影片 + 雙字幕（主要語言字幕／中英雙語／純中文字幕／RARBG 英文＋SDH／RARBG 英文＋外部中文）")
    print("2. MKV 新增一條{}字幕，強制使用這條新字幕作為唯一預設".format(primary_name))
    print("3. MKV 新增一條字幕，並自動將{}字幕設為唯一預設（可在原內嵌軌或新外掛軌）".format(primary_name))
    print("4. MKV 只新增一條字幕，原有字幕 Default 設定完全不變")
    mode_choice = input("輸入 1、2、3 或 4：").strip()
    if mode_choice not in ("1", "2", "3", "4"):

        print("\n未選擇有效模式，已安全退出。")
        input("按 Enter 結束……")
        return 1

    if mode_choice == "1":
        print("\n工作模式 1 的字幕組合：")
        print("1. 已有外掛中文＋原語言雙語字幕：再加入主要語言字幕（{}；需提供兩個字幕資料夾；來源影片內嵌字幕不會保留）".format(primary_name))
        print("2. 無內嵌字幕影片：新增主要語言字幕（{}）+ 外掛純中文字幕（需提供兩個字幕資料夾）".format(primary_name))
        print("3. RARBG Subs：普通 English + English SDH（僅 English 語言設定）")
        print("4. RARBG Subs 普通 English + 外部中文字幕（僅 English 語言設定；不加入 SDH）")
        print("說明：組合 1 的中英雙語字幕是另外選擇的外掛字幕檔，不是保留影片內嵌字幕；若中文／雙語字幕已內嵌在原始 MKV，請勿選 1。")
        print("說明：只有純中文字幕外掛檔時選組合 2；已有內嵌字幕且要保留時，請使用工作模式 2／3／4 的追加字幕流程。")
        dual_option = input("輸入 1、2、3 或 4（預設 1）：").strip() or "1"
        if dual_option not in {"1", "2", "3", "4"}:
            print("\n未選擇有效組合，已安全退出。")
            input("按 Enter 結束……")
            return 1
        if dual_option in {"3", "4"} and primary_language != "eng":
            print("\nRARBG 3_／4_ 英文字幕組合只適用 English profile。日劇／韓劇請改選組合 1 或 2，並提供 Japanese／Korean 主要字幕資料夾。")
            input("按 Enter 結束……")
            return 1

        try:
            video_prompt = ("1/4：選擇影片根資料夾（包含 MP4／MKV／M4V）"
                            if dual_option == "4" else
                            "1/3：選擇影片根資料夾（包含 MP4／MKV／M4V）"
                            if dual_option == "3" else
                            "1/4：選擇影片根資料夾（包含 MP4／MKV／M4V）")
            video_root = choose_folder(video_prompt)
            if dual_option == "1":
                mode = "dual"
                bilingual_root = choose_folder("2/4：選擇原始中文＋原語言雙語字幕資料夾（以 zh-TW／zh-CN 標明簡繁）")
                english_root = choose_folder("3/4：選擇主要語言字幕資料夾（{}；ASS／SSA／SRT）".format(primary_name))
                output_root = choose_folder("4/4：選擇新的輸出資料夾")
                roots = [video_root, bilingual_root, english_root]
                videos = collect(video_root, VIDEO_EXTS)
                bilingual_files = collect(bilingual_root, SUB_EXTS)
                english_files = collect(english_root, SUB_EXTS)
                v_idx, v_untagged = index_by_episode(videos)
                b_idx, b_untagged = index_by_episode(bilingual_files)
                e_idx, e_untagged = index_by_episode(english_files)
                all_keys = sorted(set(v_idx) | set(b_idx) | set(e_idx))
                jobs, problems = [], []
                for key in all_keys:
                    vs, bs, es = v_idx.get(key, []), b_idx.get(key, []), e_idx.get(key, [])
                    if len(vs) == len(bs) == len(es) == 1:
                        out = (output_root / vs[0].relative_to(video_root)).with_suffix(".mkv")
                        if out.exists(): problems.append((key, "輸出 MKV 已存在，為防覆蓋而跳過", str(out)))
                        elif existing_language_sidecar_paths(out, primary_language) or existing_chinese_sidecar_paths(out):
                            existing = existing_language_sidecar_paths(out, primary_language) + existing_chinese_sidecar_paths(out)
                            problems.append((key, "同名主要語言／中文字幕旁掛檔已存在，為防覆蓋而跳過", "\n".join(str(x) for x in existing)))
                        else: jobs.append((key, vs[0], es[0], bs[0], out))
                    else:
                        details = "影片 {} 份、中英雙語字幕 {} 份、英文字幕 {} 份".format(len(vs), len(bs), len(es))
                        files = "\n".join(str(x) for x in (vs + bs + es)) or "（沒有找到對應檔案）"
                        problems.append((key, "缺少或匹配不唯一：" + details, files))
                for label, paths in (("影片", v_untagged), ("雙語字幕", b_untagged), ("英文字幕", e_untagged)):
                    for item in paths: problems.append(("未識別集數", "{} 檔名沒有唯一 SxxExx 編號".format(label), str(item)))
                scan_summary = "影片 {} 個；中文＋原語言雙語字幕 {} 個；{}字幕 {} 個。".format(len(videos), len(bilingual_files), primary_name, len(english_files))
            elif dual_option == "2":
                mode = "dual_english_chinese"
                english_root = choose_folder("2/4：選擇主要語言字幕資料夾（{}；ASS／SSA／SRT）".format(primary_name))
                chinese_root = choose_folder("3/4：選擇純中文字幕資料夾（ASS／SSA／SRT）")
                output_root = choose_folder("4/4：選擇新的輸出資料夾")
                roots = [video_root, english_root, chinese_root]
                videos = collect(video_root, VIDEO_EXTS)
                english_files = collect(english_root, SUB_EXTS)
                chinese_files = collect(chinese_root, SUB_EXTS)
                v_idx, v_untagged = index_by_episode(videos)
                e_idx, e_untagged = index_by_episode(english_files)
                c_idx, c_untagged = index_by_episode(chinese_files)
                all_keys = sorted(set(v_idx) | set(e_idx) | set(c_idx))
                jobs, problems = [], []
                for key in all_keys:
                    vs, es, cs = v_idx.get(key, []), e_idx.get(key, []), c_idx.get(key, [])
                    if len(vs) == len(es) == len(cs) == 1:
                        out = (output_root / vs[0].relative_to(video_root)).with_suffix(".mkv")
                        if out.exists(): problems.append((key, "輸出 MKV 已存在，為防覆蓋而跳過", str(out)))
                        elif existing_language_sidecar_paths(out, primary_language) or existing_chinese_sidecar_paths(out):
                            existing = existing_language_sidecar_paths(out, primary_language) + existing_chinese_sidecar_paths(out)
                            problems.append((key, "同名主要語言／中文字幕旁掛檔已存在，為防覆蓋而跳過", "\n".join(str(x) for x in existing)))
                        else: jobs.append((key, vs[0], es[0], cs[0], out))
                    else:
                        details = "影片 {} 份、{}字幕 {} 份、純中文字幕 {} 份".format(len(vs), primary_name, len(es), len(cs))
                        files = "\n".join(str(x) for x in (vs + es + cs)) or "（沒有找到對應檔案）"
                        problems.append((key, "缺少或匹配不唯一：" + details, files))
                for label, paths in (("影片", v_untagged), (primary_name + "字幕", e_untagged), ("中文字幕", c_untagged)):
                    for item in paths: problems.append(("未識別集數", "{} 檔名沒有唯一 SxxExx 編號".format(label), str(item)))
                scan_summary = "影片 {} 個；{}字幕 {} 個；純中文字幕 {} 個。來源若含內嵌字幕，處理時會安全停止該集。".format(
                    len(videos), primary_name, len(english_files), len(chinese_files))
            elif dual_option == "3":
                if primary_language != "eng":
                    raise RuntimeError("RARBG Subs 的 3_／4_ 英文字幕模式只適用 English 語言設定；日劇／韓劇請使用主要語言字幕資料夾選項。")
                mode = "rarbg_english_sdh"
                rarbg_subs_root = choose_folder("2/3：選擇 RARBG 的 Subs 資料夾（內含每集同名子資料夾）")
                output_root = choose_folder("3/3：選擇新的輸出資料夾")
                roots = [video_root, rarbg_subs_root]
                videos = collect(video_root, VIDEO_EXTS)
                v_idx, v_untagged = index_by_episode(videos)
                jobs, problems = [], []
                rarbg_match_notes = []
                for key in sorted(v_idx):
                    vs = v_idx.get(key, [])
                    if len(vs) != 1:
                        problems.append((key, "影片集數重複，無法唯一配對：{} 份".format(len(vs)),
                                         "\n".join(str(x) for x in vs)))
                        continue
                    video = vs[0]
                    try:
                        english_sub, sdh_sub, match_note = select_rarbg_english_and_sdh(video, rarbg_subs_root)
                    except Exception as exc:
                        problems.append((key, "RARBG 字幕配對失敗：{}".format(exc), str(video)))
                        continue
                    out = (output_root / video.relative_to(video_root)).with_suffix(".mkv")
                    if out.exists():
                        problems.append((key, "輸出 MKV 已存在，為防覆蓋而跳過", str(out)))
                    elif (existing_english_sidecar_paths(out) or existing_english_sdh_sidecar_paths(out)
                          or existing_chinese_sidecar_paths(out)):
                        existing = (existing_english_sidecar_paths(out) + existing_english_sdh_sidecar_paths(out)
                                    + existing_chinese_sidecar_paths(out))
                        problems.append((key, "同名英文／SDH／中文字幕旁掛檔已存在，為防覆蓋而跳過",
                                         "\n".join(str(x) for x in existing)))
                    else:
                        jobs.append((key, video, english_sub, sdh_sub, out))
                        rarbg_match_notes.append((key, match_note))
                        print("RARBG 配對預覽 {}：{}".format(key, match_note), flush=True)
                for item in v_untagged:
                    problems.append(("未識別集數", "影片檔名沒有唯一 SxxExx 編號", str(item)))
                matched_video_keys = set(v_idx)
                orphan_folders = []
                for folder in rarbg_subs_root.iterdir():
                    if folder.is_dir() and episode_key(folder) and episode_key(folder) not in matched_video_keys:
                        orphan_folders.append(folder)
                for folder in orphan_folders:
                    problems.append((episode_key(folder) or "未知集數", "Subs 中存在未匹配到影片的字幕資料夾", str(folder)))
                scan_summary = "影片 {} 個；成功配對 RARBG 普通英文＋SDH {} 組；需檢查 {} 項。".format(
                    len(videos), len(jobs), len(problems))
            else:
                if primary_language != "eng":
                    raise RuntimeError("RARBG 英文＋外部中文字幕模式只適用 English 語言設定；日劇／韓劇請使用主要語言字幕資料夾選項。")
                mode = "rarbg_english_chinese"
                rarbg_subs_root = choose_folder("2/4：選擇 RARBG 的 Subs 資料夾（內含每集同名子資料夾）")
                chinese_root = choose_folder("3/4：選擇外部中文字幕資料夾（檔名需含唯一 SxxEyy）")
                output_root = choose_folder("4/4：選擇新的輸出資料夾")
                roots = [video_root, rarbg_subs_root, chinese_root]
                videos = collect(video_root, VIDEO_EXTS)
                chinese_files = collect(chinese_root, SUB_EXTS)
                v_idx, v_untagged = index_by_episode(videos)
                c_idx, c_untagged = index_by_episode(chinese_files)
                jobs, problems = [], []
                rarbg_match_notes = []
                for key in sorted(set(v_idx) | set(c_idx)):
                    vs, cs = v_idx.get(key, []), c_idx.get(key, [])
                    if len(vs) != 1 or len(cs) != 1:
                        problems.append((key, "影片或中文字幕缺少／重複：影片 {} 份、中文字幕 {} 份".format(
                            len(vs), len(cs)), "\n".join(str(x) for x in vs + cs)))
                        continue
                    video, chinese_sub = vs[0], cs[0]
                    try:
                        english_sub, match_note = select_rarbg_standard_english(video, rarbg_subs_root)
                    except Exception as exc:
                        problems.append((key, "RARBG 普通英文字幕配對失敗：{}".format(exc), str(video)))
                        continue
                    out = (output_root / video.relative_to(video_root)).with_suffix(".mkv")
                    existing_sidecars = existing_english_sidecar_paths(out) + existing_chinese_sidecar_paths(out)
                    if out.exists():
                        problems.append((key, "輸出 MKV 已存在，為防覆蓋而跳過", str(out)))
                    elif existing_sidecars:
                        problems.append((key, "同名英文／中文字幕旁掛檔已存在，為防覆蓋而跳過",
                                         "\n".join(str(x) for x in existing_sidecars)))
                    else:
                        jobs.append((key, video, english_sub, chinese_sub, out))
                        rarbg_match_notes.append((key, match_note))
                        print("RARBG＋中文字幕配對預覽 {}：英文 {}；中文 {}；{}".format(
                            key, english_sub.name, chinese_sub.name, match_note), flush=True)
                for item in v_untagged:
                    problems.append(("未識別集數", "影片檔名沒有唯一 SxxEyy 編號", str(item)))
                for item in c_untagged:
                    problems.append(("未識別集數", "中文字幕檔名沒有唯一 SxxEyy 編號", str(item)))
                matched_video_keys = set(v_idx)
                for folder in rarbg_subs_root.iterdir():
                    if folder.is_dir() and episode_key(folder) and episode_key(folder) not in matched_video_keys:
                        problems.append((episode_key(folder), "RARBG Subs 中存在未匹配影片的字幕資料夾", str(folder)))
                scan_summary = "影片 {} 個；外部中文字幕 {} 個；成功配對 RARBG 普通英文＋中文字幕 {} 組；需檢查 {} 項。來源有內嵌字幕時會安全停止該集。".format(
                    len(videos), len(chinese_files), len(jobs), len(problems))
        except RuntimeError as exc:
            print("\n" + str(exc)); input("按 Enter 結束……"); return 1

    else:
        mode = {
            "2": "append_english_default",
            "3": "append_english_auto_default",
            "4": "append_preserve",
        }[mode_choice]
        try:
            video_root = choose_folder("1/3：選擇原始 MKV 根資料夾（只掃描 .mkv）")
            subtitle_root = choose_folder("2/3：選擇準備新增的外掛字幕資料夾（ASS／SSA／SRT）")
            output_root = choose_folder("3/3：選擇新的輸出資料夾")
        except RuntimeError as exc:
            print("\n" + str(exc)); input("按 Enter 結束……"); return 1
        roots = [video_root, subtitle_root]
        videos = collect(video_root, {".mkv"})
        subtitles = collect(subtitle_root, SUB_EXTS)
        v_idx, v_untagged = index_by_episode(videos)
        s_idx, s_untagged = index_by_episode(subtitles)
        all_keys = sorted(set(v_idx) | set(s_idx))
        jobs, problems = [], []
        for key in all_keys:
            vs, ss = v_idx.get(key, []), s_idx.get(key, [])
            if len(vs) == 1 and len(ss) == 1:
                out = (output_root / vs[0].relative_to(video_root)).with_suffix(".mkv")
                if out.exists(): problems.append((key, "輸出 MKV 已存在，為防覆蓋而跳過", str(out)))
                elif existing_language_sidecar_paths(out, primary_language) or existing_chinese_sidecar_paths(out):
                    existing = existing_language_sidecar_paths(out, primary_language) + existing_chinese_sidecar_paths(out)
                    problems.append((key, "同名主要語言／中文字幕旁掛檔已存在，為防覆蓋而跳過", "\n".join(str(x) for x in existing)))
                else: jobs.append((key, vs[0], ss[0], out))
            else:
                details = "MKV {} 份、外掛字幕 {} 份".format(len(vs), len(ss))
                files = "\n".join(str(x) for x in (vs + ss)) or "（沒有找到對應檔案）"
                problems.append((key, "缺少或匹配不唯一：" + details, files))
        for label, paths in (("MKV", v_untagged), ("外掛字幕", s_untagged)):
            for item in paths: problems.append(("未識別集數", "{} 檔名沒有唯一 SxxExx 編號".format(label), str(item)))
        scan_summary = "MKV {} 個；外掛字幕 {} 個。".format(len(videos), len(subtitles))
        if mode == "append_english_default":
            new_language, new_name = primary_language, primary_name
        else:
            new_language = input("新增字幕語言代碼（預設 {}）：".format(primary_language)).strip() or primary_language
            new_name = input("新增字幕軌名稱（預設 {}）：".format(primary_name)).strip() or primary_name
            if not re.fullmatch(r"[A-Za-z0-9-]{2,35}", new_language):
                print("語言代碼格式不正確，已安全退出。")
                input("按 Enter 結束……")
                return 1

    output_root = output_root.resolve()
    if any(is_within(output_root, root.resolve()) for root in roots):
        print("\n安全停止：輸出資料夾不能位於任何來源資料夾內。請另建獨立輸出資料夾。")
        input("按 Enter 結束……")
        return 2
    output_root.mkdir(parents=True, exist_ok=True)
    logfile = output_root / ("FRAME_MKV_Batch_{}.log".format(datetime.now().strftime("%Y%m%d_%H%M%S_%f")))
    with logfile.open("w", encoding="utf-8") as f:
        f.write("FRAME MKV Batch Log v{}\nStarted: {}\n".format(VERSION, datetime.now().isoformat(timespec="seconds")))
        f.write("mkvmerge: {}\nmkvextract: {}\nffmpeg: {}\nffprobe: {}\n\n".format(mkvmerge, mkvextract, ffmpeg, ffprobe))
        f.write("主要字幕語言：{}（{}）；旁掛副檔名 {}；另嘗試匯出唯一可識別的中文字幕／中英雙語字幕為同名 .zh。\n".format(
            primary_name, primary_language, primary_profile["suffix"]))
        f.write("音訊篩選：僅保留 eng／jpn／kor；語言未知／衝突時停止該集。\n")
        f.write("字幕模式：{}\n".format({
            "dual": "影片＋主要語言字幕＋中英雙語字幕",
            "dual_english_chinese": "無內嵌字幕影片＋主要語言字幕＋純中文字幕",
            "rarbg_english_sdh": "RARBG Subs 每集資料夾＋普通英文＋English SDH",
            "rarbg_english_chinese": "RARBG Subs 普通英文＋外部中文字幕",
            "append_english_default": "MKV 新增外掛主要語言字幕並強制設為預設",
            "append_english_auto_default": "MKV 新增字幕並自動選擇主要語言作為預設",
            "append_preserve": "MKV 新增字幕且保留原 Default 設定",
        }[mode]))
        f.write("影片處理：{}\n".format("重新編碼影片再封裝" if transcode_settings else "無損封裝，不重新編碼"))
        if transcode_settings:
            f.write("壓制設定：{}\n".format(json.dumps(transcode_settings, ensure_ascii=False, default=str)))

    write_log(logfile, "掃描結果：" + scan_summary)
    if mode in ("rarbg_english_sdh", "rarbg_english_chinese"):
        write_log(logfile, "RARBG 字幕分類依據（不是依 3_／4_ 序號判斷）：")
        for match_key, match_note in rarbg_match_notes:
            write_log(logfile, "[{}] {}".format(match_key, match_note))
    write_log(logfile, "可封裝 {} 項；跳過／需人工檢查 {} 項。".format(len(jobs), len(problems)))
    if problems:
        write_log(logfile, "\n跳過／需人工檢查明細：")
        for key, reason, detail in problems:
            write_log(logfile, "[{}] {}".format(key, reason))
            if detail: write_log(logfile, detail)
    else:
        write_log(logfile, "\n沒有發現缺檔、重複配對或未識別集數。")

    write_log(logfile, "\n配對預覽：")
    for job in jobs:
        if mode in ("dual", "dual_english_chinese", "rarbg_english_sdh", "rarbg_english_chinese"):
            key, video, eng, second_sub, out = job
            second_label = ("雙語" if mode == "dual" else
                            "SDH 英文" if mode == "rarbg_english_sdh" else "中文字幕")
            primary_label = "普通 English" if mode in ("rarbg_english_sdh", "rarbg_english_chinese") else primary_name
            write_log(logfile, "[{}]\n影片：{}\n{}：{}\n{}：{}\n輸出：{}".format(
                key, video, primary_label, eng, second_label, second_sub, out))
        else:
            key, video, subtitle, out = job
            write_log(logfile, "[{}]\n來源 MKV：{}\n新增字幕：{}\n輸出：{}".format(key, video, subtitle, out))

    print("\n" + "=" * 72)
    print("掃描結果：" + scan_summary)
    print("可封裝：{} 項；跳過／需人工檢查：{} 項".format(len(jobs), len(problems)))
    if problems:
        print("\n跳過／需人工檢查明細（同時保存至日誌）：")
        for key, reason, detail in problems:
            print("[{}] {}".format(key, reason))
            if detail: print("  " + detail.replace("\n", "\n  "))
    print("\n配對預覽：")
    for job in jobs:
        if mode in ("dual", "dual_english_chinese", "rarbg_english_sdh", "rarbg_english_chinese"):
            key, video, eng, second_sub, out = job
            second_label = ("雙語" if mode == "dual" else
                            "SDH 英文" if mode == "rarbg_english_sdh" else "中文字幕")
            primary_label = "普通 English" if mode in ("rarbg_english_sdh", "rarbg_english_chinese") else primary_name
            print("\n[{}]\n影片：{}\n{}：{}\n{}：{}\n輸出：{}".format(
                key, video, primary_label, eng, second_label, second_sub, out))
        else:
            key, video, subtitle, out = job
            print("\n[{}]\n來源 MKV：{}\n新增字幕：{}\n輸出：{}".format(key, video, subtitle, out))
    print("\n日誌：{}".format(logfile))

    if not jobs:
        print("\n沒有可安全封裝的配對。")
        input("按 Enter 結束……")
        return 1

    if mode == "dual":
        description = "來源影片＋主要語言字幕（{}）＋中英雙語字幕；輸出時排除來源內嵌字幕，主要語言字幕設為預設。".format(primary_name)
    elif mode == "dual_english_chinese":
        description = "僅適用無內嵌字幕來源；新增 {}（{}，Default Yes）及 Chinese（zh，Default No），拒絕丟棄來源內嵌字幕。".format(primary_name, primary_language)
    elif mode == "rarbg_english_sdh":
        description = "從 RARBG Subs 每集子資料夾自動辨識普通英文與 SDH；普通 English（eng，Default Yes），English SDH（eng，Default No）；來源需無內嵌字幕。"
    elif mode == "rarbg_english_chinese":
        description = "從 RARBG Subs 每集子資料夾選取唯一普通英文，並按 SxxEyy 配對外部中文字幕；English（eng，Default Yes），Chinese（zh，Default No）；來源需無內嵌字幕。"
    elif mode == "append_english_default":
        description = "保留 MKV 原有字幕內容，但將原有字幕 Default 關閉，新增 {} 設為唯一預設字幕。".format(primary_name)
    elif mode == "append_english_auto_default":
        description = "保留 MKV 原有字幕；在原有內嵌字幕與本次新增字幕中辨識 {}，將唯一選定的{}字幕設為 Default，其餘字幕關閉。多條目標語言且無唯一既有預設時會停止該集，不猜測。".format(primary_name, primary_name)
    else:
        description = "保留 MKV 原有字幕內容及每條字幕的 Default 設定；新增字幕明確設為非預設。"
    print("\n字幕模式說明：" + description)
    if transcode_settings:
        print("影片處理：影片會重新編碼；音訊／字幕／章節／附件由 MKVToolNix 直接複製。")
        print("壓制不是無損操作；來源永不覆蓋，輸出另存新 MKV。")
        print("注意：暫存工作檔位於所選輸出資料夾所在磁碟，請預留足夠空間。")
    else:
        print("影片處理：純無損封裝；不重新編碼影片或音訊。")
    print("每個 MKV 完整驗證及正式發布後，都會抽取主要語言字幕 {}；中文／雙語旁掛檔依軌道標記命名，例如 .zh-tw-en.ass、.zh-cn-ja.ass。多條中文候選且無唯一 Default、語言標籤矛盾或格式不支援時不猜測、不覆蓋。".format(primary_profile["suffix"]))
    print("音訊篩選：只保留 English／Japanese／Korean；被排除語言不會進入最終 MKV。")
    if mode == "dual_english_chinese":
        print("字幕軌設定：{}（{}，Default Yes）；Chinese（zh，Default No）。".format(primary_name, primary_language))
        print("安全限制：若來源影片已有任何內嵌字幕軌，本模式會停止該集，不會將它們靜默丟棄。")
    elif mode == "rarbg_english_sdh":
        print("字幕軌設定：普通 English（eng，Default Yes）；English SDH（eng，Default No）。")
        print("英文旁掛檔：輸出 .en.srt／.en.ass；SDH 另輸出 .en.sdh.srt／.en.sdh.ass。")
        print("配對依據：優先比對影片完整檔名與 Subs 子資料夾名稱，再以字幕內容辨識 SDH；不依 3_／4_ 編號猜測。")
        print("安全限制：來源影片若已有任何內嵌字幕軌，會停止該集，不會靜默丟棄。")
    elif mode == "rarbg_english_chinese":
        print("字幕軌設定：RARBG 普通 English（eng，Default Yes）；外部 Chinese（zh，Default No）。")
        print("英文來源：RARBG Subs 每集子資料夾；中文字幕來源：獨立資料夾，依唯一 SxxEyy 配對。")
        print("English SDH 若存在會記錄但不加入本模式；若要同時加入 SDH，請使用 1C。")
        print("安全限制：來源影片若已有任何內嵌字幕軌，會停止該集，不會靜默丟棄。")
    elif mode not in ("dual",):
        print("新增字幕標籤：language={}；track name={}。".format(new_language, new_name))
    answer = input("確認開始處理全部 {} 項？輸入大寫 YES 才會開始：".format(len(jobs))).strip()
    if answer != "YES":
        write_log(logfile, "\n使用者取消。掃描與配對明細已保存。")
        print("\n已取消，沒有建立 MKV。")
        input("按 Enter 結束……")
        return 0

    write_log(logfile, "\n開始封裝與完整驗證……")
    ok_count = warning_count = fail_count = 0
    sidecar_export_count = sidecar_not_exported_count = 0
    chinese_sidecar_export_count = chinese_sidecar_not_exported_count = 0
    sdh_sidecar_export_count = sdh_sidecar_not_exported_count = 0
    for i, job in enumerate(jobs, 1):
        prepared_source = None
        original_source = None
        staging_dir = None
        staged_output = None
        published = False
        out = None
        key = "未知集數"
        try:
            if mode in ("dual", "dual_english_chinese", "rarbg_english_sdh", "rarbg_english_chinese"):
                key, video, eng, second_sub, out = job
                original_source = video
                second_label = ("雙語" if mode == "dual" else
                                "SDH 英文" if mode == "rarbg_english_sdh" else "中文字幕")
                write_log(logfile, "\n[{}/{}] {}".format(i, len(jobs), key))
                primary_label = "普通 English" if mode in ("rarbg_english_sdh", "rarbg_english_chinese") else primary_name
                write_log(logfile, "影片：{}\n{}：{}\n{}：{}\n輸出：{}".format(
                    video, primary_label, eng, second_label, second_sub, out))
            else:
                key, video, subtitle, out = job
                original_source = video
                write_log(logfile, "\n[{}/{}] {}".format(i, len(jobs), key))
                write_log(logfile, "來源 MKV：{}\n新增字幕：{}\n輸出：{}".format(video, subtitle, out))

            # Build and validate in a hidden staging directory on the destination
            # volume. The official filename is created only after validation passes.
            out.parent.mkdir(parents=True, exist_ok=True)
            staging_dir = Path(tempfile.mkdtemp(prefix=".frame-v2-staging-", dir=str(out.parent)))
            staged_output = staging_dir / out.name
            if out.exists():
                raise FileExistsError("正式輸出在工作開始前已出現，拒絕覆蓋：{}".format(out))

            processing_source = video
            if transcode_settings:
                prepared_source, was_encoded, encode_message = encode_video_and_prepare_source(
                    ffmpeg, ffprobe, mkvmerge, mkvextract, video, output_root,
                    transcode_settings, logfile, key)
                processing_source = prepared_source
                write_log(logfile, "影片處理結果：{}。".format(encode_message))

            if mode == "dual":
                ok, message = mux_one(mkvmerge, mkvextract, ffmpeg, ffprobe,
                                      processing_source, eng, second_sub, staged_output, logfile,
                                      primary_language=primary_language, primary_track_name=primary_name)
            elif mode == "dual_english_chinese":
                ok, message = mux_english_chinese_only(
                    mkvmerge, mkvextract, ffmpeg, ffprobe,
                    processing_source, eng, second_sub, staged_output, logfile,
                    primary_language=primary_language, primary_track_name=primary_name)
            elif mode == "rarbg_english_sdh":
                ok, message = mux_rarbg_english_sdh(
                    mkvmerge, mkvextract, ffmpeg, ffprobe,
                    processing_source, eng, second_sub, staged_output, logfile)
            elif mode == "rarbg_english_chinese":
                ok, message = mux_english_chinese_only(
                    mkvmerge, mkvextract, ffmpeg, ffprobe,
                    processing_source, eng, second_sub, staged_output, logfile)
            else:
                ok, message = mux_append_one(mkvmerge, mkvextract, ffmpeg, ffprobe,
                                             processing_source, subtitle, staged_output,
                                             logfile, mode, new_language, new_name,
                                             primary_language=primary_language)

            if ok:
                atomic_publish_no_overwrite(staged_output, out)
                published = True
                write_log(logfile, "正式輸出已原子發布（僅在完整驗證成功後）：{}".format(out))
            else:
                try:
                    failed_path = preserve_failed_staging(staged_output, out, logfile, message)
                except Exception as preserve_error:
                    failed_path = None
                    message += "；未驗證暫存檔無法移至 Failed，暫存目錄將保留：{}".format(preserve_error)
                if failed_path:
                    message += "；未驗證檔已移至 Failed/{}".format(failed_path.name)

        except KeyboardInterrupt:
            if staged_output and out and not published:
                try:
                    preserve_failed_staging(staged_output, out, logfile, "使用者中斷／Ctrl+C")
                except Exception as preserve_error:
                    write_log(logfile, "Ctrl+C 後未驗證檔未能移至 Failed，原暫存目錄保留：{}".format(preserve_error))
            if prepared_source and original_source and Path(prepared_source) != Path(original_source):
                cleanup_prepared_source(prepared_source)
            raise
        except Exception as exc:
            ok, message = False, "壓制／準備／封裝／發布失敗：{}".format(exc)
            if staged_output and out and not published:
                try:
                    preserve_failed_staging(staged_output, out, logfile, message)
                except Exception as preserve_error:
                    message += "；未驗證暫存檔未能移至 Failed，原暫存目錄保留：{}".format(preserve_error)
        finally:
            if prepared_source and original_source and Path(prepared_source) != Path(original_source):
                cleanup_prepared_source(prepared_source)
            if staging_dir and staging_dir.exists():
                cleanup_empty_staging_directory(staging_dir, logfile)

        if ok and published:
            sidecar_ok, sidecar_message, sidecar_status = export_english_sidecar(
                mkvextract, mkvmerge, ffprobe, out, logfile, target_language=primary_language)
            if sidecar_ok:
                sidecar_export_count += 1
                message += "；預設語言字幕旁掛檔已輸出：{}".format(Path(sidecar_message).name)
            else:
                sidecar_not_exported_count += 1
                message += "；預設語言字幕旁掛檔未匯出：{}".format(sidecar_message)
            zh_ok, zh_message, zh_status = export_chinese_sidecar(
                mkvextract, mkvmerge, ffprobe, out, logfile)
            if zh_ok:
                chinese_sidecar_export_count += 1
                message += "；中文字幕／中英雙語旁掛檔已輸出：{}".format(Path(zh_message).name)
            else:
                chinese_sidecar_not_exported_count += 1
                message += "；中文字幕／中英雙語旁掛檔未匯出：{}".format(zh_message)
            if mode == "rarbg_english_sdh":
                sdh_ok, sdh_message, sdh_status = export_english_sidecar(
                    mkvextract, mkvmerge, ffprobe, out, logfile, sdh=True)
                if sdh_ok:
                    sdh_sidecar_export_count += 1
                    message += "；SDH 旁掛檔已輸出：{}".format(Path(sdh_message).name)
                else:
                    sdh_sidecar_not_exported_count += 1
                    message += "；SDH 旁掛檔未匯出：{}".format(sdh_message)
            ok_count += 1
            if "警告" in message or "診斷訊息" in message or "未匯出" in message:
                warning_count += 1
                write_log(logfile, "成功（有診斷訊息）：{}：{}".format(key, message))
                print("成功（有診斷訊息）：{}".format(key), flush=True)
            else:
                write_log(logfile, "成功（完整驗證通過）：{}：{}".format(key, message))
                print("成功：{}".format(key), flush=True)
        else:
            fail_count += 1
            write_log(logfile, "失敗／需檢查：{}：{}".format(key, message))
            print("失敗／需檢查：{}：{}".format(key, message), flush=True)

    write_log(logfile, "\n完成：驗證通過 {} 項（有診斷訊息 {} 項），失敗／需檢查 {} 項，跳過／人工檢查 {} 項。".format(
        ok_count, warning_count, fail_count, len(problems)))
    write_log(logfile, "主要語言字幕旁掛檔：成功匯出 {} 項；未能匯出 {} 項。".format(
        sidecar_export_count, sidecar_not_exported_count))
    write_log(logfile, "中文字幕／中英雙語 .zh 旁掛檔：成功匯出 {} 項；未能匯出 {} 項（可能沒有中文軌、多條候選無法唯一判斷、編碼不支援或匯出錯誤）。".format(
        chinese_sidecar_export_count, chinese_sidecar_not_exported_count))
    if mode == "rarbg_english_sdh":
        write_log(logfile, "SDH 英文旁掛檔：成功匯出 {} 項；未能匯出 {} 項。".format(
            sdh_sidecar_export_count, sdh_sidecar_not_exported_count))
    print("\n全部完成。驗證通過：{}（有診斷訊息 {}），失敗／需檢查：{}，跳過／人工檢查：{}。".format(
        ok_count, warning_count, fail_count, len(problems)))
    print("主要語言字幕旁掛檔：成功匯出 {} 項；未能匯出 {} 項。".format(
        sidecar_export_count, sidecar_not_exported_count))
    print("中文字幕／中英雙語 .zh 旁掛檔：成功匯出 {} 項；未能匯出 {} 項。".format(
        chinese_sidecar_export_count, chinese_sidecar_not_exported_count))
    print("詳細日誌：{}".format(logfile))
    input("\n按 Enter 結束……")
    return 0 if fail_count == 0 else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n已由使用者中止。")
        sys.exit(130)
