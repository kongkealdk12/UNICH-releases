"""U'NICH AI GATEWAY v2.1 — 3-in-1 Pipeline Client.

Supports:
1. Transcribe: POST /v1/audio/transcriptions (with [Male]/[Female] prompt tags)
2. Gender Diarization: Extracted from transcript speaker tags
3. Gender-Adaptive Translation: POST /v1/chat/completions (Strict Male -> 'បាទ', Female -> 'ចាស')
4. Model Rotation Pool: Automatic failover on 429 / 500 / timeout across 5 ranked models.
"""

import json
import os
import re
import subprocess
import tempfile
import time
import requests

import sys

# Safe config resolution for both dev environment and Nuitka compiled binary
config = sys.modules.get('AIVideoTranslator.config') or sys.modules.get('config')
if not config:
    try:
        import AIVideoTranslator.config as config
    except (ImportError, ValueError):
        try:
            from .. import config
        except (ImportError, ValueError):
            try:
                from . import config
            except (ImportError, ValueError):
                try:
                    import config
                except (ImportError, ValueError):
                    config = None

if not config:
    class _FallbackConfig:
        UNICH_BASE_URL = 'https://api.nisay.store/v1'
        NISAY_BASE_URL = 'https://api.nisay.store/v1'
        UNICH_ROTATE_POOL = [
            'gemini-3.8-flash-high',
            'gemini-3.7-flash-high',
            'gpt-5.6-terra',
            'gemini-3.6-flash-high',
            'gpt-5.6-luna'
        ]
        NISAY_ROTATE_POOL = UNICH_ROTATE_POOL
        UPDATE_REPO = 'kongkealdk12/UNICH-releases'
    config = _FallbackConfig()

UNICH_DEFAULT_BASE_URL = getattr(config, 'UNICH_BASE_URL', getattr(config, 'NISAY_BASE_URL', 'https://api.nisay.store/v1'))
NISAY_DEFAULT_BASE_URL = UNICH_DEFAULT_BASE_URL

DEFAULT_ROTATE_POOL = getattr(config, 'UNICH_ROTATE_POOL', getattr(config, 'NISAY_ROTATE_POOL', [
    'gemini-3.8-flash-high',
    'gemini-3.7-flash-high',
    'gpt-5.6-terra',
    'gemini-3.6-flash-high',
    'gpt-5.6-luna'
]))
UNICH_ROTATE_POOL = DEFAULT_ROTATE_POOL
NISAY_ROTATE_POOL = DEFAULT_ROTATE_POOL


def _sanitize_gateway_error(err):
    """Sanitize error messages so internal API hostnames or provider names are masked for end-users."""
    if not err:
        return ""
    s = str(err)
    s = re.sub(r'https?://[^\s/$.?#].[^\s]*nisay[^\s]*', "https://gateway.unich.vip/v1", s, flags=re.IGNORECASE)
    s = re.sub(r'api\.nisay\.store', "gateway.unich.vip", s, flags=re.IGNORECASE)
    s = re.sub(r'nisay[-_\s]*transcribe', "unich-transcribe", s, flags=re.IGNORECASE)
    s = re.sub(r'\bnisay\b', "U'Nich", s, flags=re.IGNORECASE)
    return s


class UNichGatewayError(Exception):
    """Base exception for U'Nich AI Gateway errors."""
    pass


# Backward-compatible backend alias
NisayGatewayError = UNichGatewayError


class UNichKeyExpiredError(UNichGatewayError):
    """Raised when the U'Nich AI Gateway returns 403 Forbidden."""
    pass


# Backward-compatible backend alias
NisayKeyExpiredError = UNichKeyExpiredError


def post_with_retry(url, pool, api_key, base_url=None, cancel_check=None, timeout=180, **kwargs):
    """POST request with automatic model rotation across candidate pool."""
    if not pool:
        pool = DEFAULT_ROTATE_POOL
    base_url = (base_url or UNICH_DEFAULT_BASE_URL).rstrip('/')
    if not url.startswith('http'):
        url = f"{base_url}/{url.lstrip('/')}"

    headers = dict(kwargs.get('headers') or {})
    headers['Authorization'] = f"Bearer {api_key}"

    last_error = None
    for i, model in enumerate(pool):
        if cancel_check and cancel_check():
            raise UNichGatewayError("Cancelled by user.")

        try:
            call_kwargs = dict(kwargs)
            call_kwargs['headers'] = headers
            call_kwargs['timeout'] = timeout

            # Ensure file streams are rewound before each retry attempt
            if 'files' in call_kwargs and isinstance(call_kwargs['files'], dict):
                for fk, fv in call_kwargs['files'].items():
                    if hasattr(fv, 'seek'):
                        try:
                            fv.seek(0)
                        except Exception:
                            pass
                    elif isinstance(fv, tuple) and len(fv) >= 2 and hasattr(fv[1], 'seek'):
                        try:
                            fv[1].seek(0)
                        except Exception:
                            pass

            # Model injection into JSON payload if present
            if 'json' in call_kwargs and isinstance(call_kwargs['json'], dict):
                payload = dict(call_kwargs['json'])
                payload['model'] = model
                call_kwargs['json'] = payload
            elif 'data' in call_kwargs and isinstance(call_kwargs['data'], dict):
                data = dict(call_kwargs['data'])
                data['model'] = model
                call_kwargs['data'] = data

            resp = requests.post(url, **call_kwargs)

            if resp.status_code == 403:
                raise UNichKeyExpiredError(
                    "403 Forbidden: U'Nich VIP License is invalid or expired. Please contact support.")

            if resp.status_code == 429:
                # Rate limited -> sleep and rotate to next model
                sleep_s = 2 * (i + 1)
                time.sleep(sleep_s)
                continue

            if resp.status_code == 422:
                # If audio transcription returns no_timestamped_speech, it's valid media without spoken dialogue
                try:
                    err_json = resp.json()
                    err_obj = err_json.get('error', {}) if isinstance(err_json, dict) else {}
                    if err_obj.get('code') == 'no_timestamped_speech' or 'no timestamped speech' in str(err_obj.get('message', '')).lower():
                        mock_resp = requests.Response()
                        mock_resp.status_code = 200
                        mock_resp._content = b'{"text":"","segments":[],"language":"auto"}'
                        return mock_resp
                except Exception:
                    pass

            if resp.status_code >= 500 or resp.status_code == 422:
                # Server error or payload rejection -> rotate to next model
                clean_err_text = _sanitize_gateway_error(resp.text[:140])
                last_error = f"Server status {resp.status_code}: {clean_err_text}"
                continue

            resp.raise_for_status()
            return resp

        except (UNichKeyExpiredError, NisayKeyExpiredError):
            raise
        except Exception as exc:
            last_error = exc
            if i == len(pool) - 1:
                break
            time.sleep(1.0)

    clean_last_err = _sanitize_gateway_error(last_error)
    raise UNichGatewayError(f"All models in rotation pool failed: {clean_last_err}")


def _get_ffmpeg():
    """Locate the authoritative FFmpeg binary bundled or available on the system."""
    try:
        from .translator import _ffmpeg_exe_path
        return _ffmpeg_exe_path()
    except Exception:
        import shutil
        return shutil.which("ffmpeg") or "ffmpeg"


def _prepare_upload_audio(audio_path):
    """Ensure audio file is compact and fast for upload to U'Nich AI Gateway."""
    try:
        size_mb = os.path.getsize(audio_path) / (1024 * 1024)
        is_already_compact = (
            audio_path.lower().endswith(('.mp3', '.m4a', '.webm', '.aac', '.ogg'))
            and size_mb <= 24.0
        )
        if is_already_compact:
            return audio_path, False

        import subprocess
        import tempfile
        ffmpeg_bin = _get_ffmpeg()
        out_mp3 = os.path.join(tempfile.gettempdir(), f"unich_opt_{int(time.time())}_{os.path.basename(audio_path)}.mp3")
        cmd = [
            ffmpeg_bin, "-y", "-i", audio_path,
            "-vn", "-ar", "16000", "-ac", "1", "-b:a", "64k",
            out_mp3
        ]
        res = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60)
        if res.returncode == 0 and os.path.exists(out_mp3) and os.path.getsize(out_mp3) > 1024:
            return out_mp3, True
    except Exception as e:
        print(f"U'Nich AI Gateway audio upload preflight note: {_sanitize_gateway_error(e)}")
    return audio_path, False


def _parse_srt_string(srt_text):
    """Parse raw SRT subtitle text into standard segment dictionaries."""
    segments = []
    blocks = re.split(r'\n\s*\n', srt_text.strip())
    for block in blocks:
        lines = [ln.strip() for ln in block.splitlines() if ln.strip()]
        if not lines:
            continue
        # Find line with timestamp arrow -->
        arrow_idx = -1
        for idx, ln in enumerate(lines):
            if '-->' in ln:
                arrow_idx = idx
                break
        if arrow_idx == -1:
            continue
        time_part = lines[arrow_idx]
        text_part = ' '.join(lines[arrow_idx + 1:])
        m = re.match(r'(\d+):(\d+):(\d+)[,\.](\d+)\s*-->\s*(\d+):(\d+):(\d+)[,\.](\d+)', time_part)
        if m:
            h1, m1, s1, ms1, h2, m2, s2, ms2 = map(int, m.groups())
            start = h1 * 3600 + m1 * 60 + s1 + ms1 / 1000.0
            end = h2 * 3600 + m2 * 60 + s2 + ms2 / 1000.0
            segments.append({'start': start, 'end': end, 'text': text_part})
    return segments


def parse_transcript_response(data, default_lang='auto', video_duration=None, **kwargs):
    """Extract standard raw_segments and detected_language from U'Nich AI Gateway response."""
    segments = []
    detected_lang = default_lang

    if isinstance(data, str):
        data_str = data.strip()
        try:
            data = json.loads(data_str)
        except Exception:
            if '-->' in data_str:
                segments = _parse_srt_string(data_str)
            else:
                segments = [{'start': 0.0, 'end': 0.0, 'text': data_str}]

    if isinstance(data, dict):
        detected_lang = str(data.get('language') or default_lang)
        raw_segs = data.get('segments')
        if isinstance(raw_segs, list):
            segments = raw_segs
        elif 'text' in data:
            segments = [{'start': 0.0, 'end': 0.0, 'text': data['text']}]
    elif isinstance(data, list):
        segments = data

    cleaned_segments = []
    for item in segments:
        if not isinstance(item, dict):
            continue
        raw_text = str(item.get('text', '')).strip()
        start = float(item.get('start', 0.0))
        end = float(item.get('end', start + 2.0))

        # Gender extraction from item attributes or embedded tags
        gender = None
        if 'gender' in item and str(item['gender']).lower() in ('male', 'female'):
            gender = str(item['gender']).lower()
        elif 'speaker' in item:
            spk = str(item['speaker']).lower()
            if 'female' in spk or 'ស្រី' in spk:
                gender = 'female'
            elif 'male' in spk or 'ប្រុស' in spk:
                gender = 'male'
        else:
            lower_t = raw_text.lower()
            if '[speaker 2 - female]' in lower_t or '[female]' in lower_t or '[ស្រី]' in lower_t:
                gender = 'female'
            elif '[speaker 1 - male]' in lower_t or '[male]' in lower_t or '[ប្រុស]' in lower_t:
                gender = 'male'

        clean_text = re.sub(r'\[Speaker\s*\d*\s*-\s*(?:Male|Female)\]', '', raw_text, flags=re.IGNORECASE)
        clean_text = re.sub(r'\[(?:Male|Female)\]', '', clean_text, flags=re.IGNORECASE)
        clean_text = re.sub(r'\[(?:ប្រុស|ស្រី)\]', '', clean_text, flags=re.IGNORECASE)
        clean_text = clean_text.strip()

        if not clean_text:
            continue

        raw_words = item.get('words') or []
        normalized_words = []
        for w in raw_words:
            if isinstance(w, dict):
                w_txt = str(w.get('text') or w.get('word') or '').strip()
                try:
                    w_st = float(w.get('start', start))
                    w_en = float(w.get('end', end))
                except (ValueError, TypeError):
                    w_st, w_en = start, end
                normalized_words.append({
                    'start': w_st,
                    'end': w_en,
                    'text': w_txt,
                    'word': w_txt
                })
            elif isinstance(w, str) and w.strip():
                normalized_words.append({
                    'start': start,
                    'end': end,
                    'text': w.strip(),
                    'word': w.strip()
                })

        cleaned_segments.append({
            'start': start,
            'end': end,
            'text': clean_text,
            'gender': gender,
            'words': normalized_words
        })

    return cleaned_segments, detected_lang


def _get_audio_duration_seconds(audio_path):
    """Retrieve audio duration in seconds using FFmpeg stderr metadata."""
    try:
        ffmpeg_bin = _get_ffmpeg()
        cmd = [ffmpeg_bin, "-i", audio_path]
        p = subprocess.run(cmd, stderr=subprocess.PIPE, stdout=subprocess.DEVNULL,
                           text=True, encoding='utf-8', errors='replace', timeout=15)
        m = re.search(r'Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)', p.stderr)
        if m:
            h, m_val, s = m.groups()
            return int(h) * 3600 + int(m_val) * 60 + float(s)
    except Exception:
        pass
    return 0.0


def _transcribe_single_audio_file(upload_file, api_key, lang='auto', model=None,
                                 base_url=None, cancel_check=None,
                                 response_format='verbose_json'):
    """Single file transcription request to U'Nich AI Gateway."""
    if model and 'gemini' in str(model).lower():
        t_pool = [model, "gemini-3.8-flash-high", "gemini-3.7-flash-high"]
    elif model:
        t_pool = [model, "gemini-3.8-flash-high", "gemini-3.7-flash-high"]
    else:
        t_pool = ["gemini-3.8-flash-high", "gemini-3.7-flash-high", "gemini-3.6-flash-high"]

    prompt = (
        "Transcribe verbatim with media-zero timestamps and stable speaker IDs. "
        "Strictly tag speaker gender in each segment as [Speaker 1 - Male] or [Speaker 2 - Female]."
    )

    with open(upload_file, 'rb') as f:
        audio_bytes = f.read()

    filename = os.path.basename(upload_file)
    resp = post_with_retry(
        "audio/transcriptions",
        pool=t_pool,
        api_key=api_key,
        base_url=base_url,
        cancel_check=cancel_check,
        files={"file": (filename, audio_bytes, "audio/mpeg")},
        data={"language": lang or "auto", "response_format": response_format, "prompt": prompt},
        timeout=300
    )

    try:
        return resp.json()
    except Exception as json_err:
        clean_err = _sanitize_gateway_error(json_err)
        raise UNichGatewayError(f"Invalid response from U'Nich AI Gateway: {clean_err}")


def transcribe_audio(audio_path, api_key, lang='auto', model=None, base_url=None, cancel_check=None, response_format='verbose_json'):
    """Execute Step 1 + 2: Transcribe + Gender Diarization via U'Nich AI Gateway.

    Automatically splits long audio (>60s) into ~50s smart chunks to prevent
    attention decay, dropped conversational exchanges, or timestamp drift.
    """
    try:
        from . import hotpatch
        patched = hotpatch.get_patched_module('nisay_gateway')
        if patched and hasattr(patched, 'transcribe_audio') and patched.transcribe_audio != transcribe_audio:
            return patched.transcribe_audio(audio_path, api_key, lang=lang, model=model, base_url=base_url, cancel_check=cancel_check)
    except Exception:
        pass

    if not os.path.isfile(audio_path):
        raise FileNotFoundError(f"Audio file not found: {audio_path}")

    # Check duration for smart cloud chunking
    total_dur = _get_audio_duration_seconds(audio_path)

    # If audio is longer than 60s, automatically chunk into ~50s pieces
    # to guarantee 100% dialogue capture across fast character debates/arguments
    if total_dur > 60.0:
        chunk_len = 50.0
        chunks = []
        curr = 0.0
        while curr < total_dur:
            end_t = min(curr + chunk_len, total_dur)
            chunks.append((curr, end_t))
            curr = end_t

        ffmpeg_bin = _get_ffmpeg()
        all_segments = []
        detected_lang = lang or 'auto'

        for i, (st, en) in enumerate(chunks):
            if cancel_check and cancel_check():
                raise UNichGatewayError("Cancelled by user.")

            chunk_file = os.path.join(
                tempfile.gettempdir(),
                f"unich_chunk_{int(time.time())}_{i}_{os.path.basename(audio_path)}.mp3"
            )
            try:
                cmd = [
                    ffmpeg_bin, "-y", "-ss", f"{st:.3f}", "-to", f"{en:.3f}",
                    "-i", audio_path, "-vn", "-ar", "16000", "-ac", "1", "-b:a", "64k",
                    chunk_file
                ]
                subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60)
                if not os.path.exists(chunk_file) or os.path.getsize(chunk_file) < 512:
                    continue

                try:
                    raw_chunk_data = _transcribe_single_audio_file(
                        chunk_file, api_key=api_key, lang=lang, model=model,
                        base_url=base_url, cancel_check=cancel_check,
                        response_format=response_format
                    )
                except Exception as chunk_err:
                    if "no_timestamped_speech" in str(chunk_err) or "No timestamped speech" in str(chunk_err):
                        continue
                    raise

                chunk_segs, chunk_lang = parse_transcript_response(
                    raw_chunk_data, default_lang=lang or 'auto', video_duration=en - st
                )
                if chunk_lang and chunk_lang != 'auto':
                    detected_lang = chunk_lang

                for s in chunk_segs:
                    s['start'] = round(st + float(s.get('start', 0.0)), 3)
                    s['end'] = round(st + float(s.get('end', 0.0)), 3)
                    for w in s.get('words', []):
                        if isinstance(w, dict):
                            w['start'] = round(st + float(w.get('start', 0.0)), 3)
                            w['end'] = round(st + float(w.get('end', 0.0)), 3)
                    all_segments.append(s)
            finally:
                if os.path.exists(chunk_file):
                    try:
                        os.remove(chunk_file)
                    except OSError:
                        pass

        return {
            'segments': all_segments,
            'language': detected_lang,
            'text': " ".join(s.get('text', '').strip() for s in all_segments if s.get('text'))
        }

    # Baseline single-pass for short audio (<=60s) or fallback
    upload_file, is_temp = _prepare_upload_audio(audio_path)
    try:
        return _transcribe_single_audio_file(
            upload_file, api_key=api_key, lang=lang, model=model,
            base_url=base_url, cancel_check=cancel_check,
            response_format=response_format
        )
    finally:
        if is_temp and os.path.exists(upload_file):
            try:
                os.remove(upload_file)
            except OSError:
                pass


def translate_gender_adaptive(segments, api_key, model=None, base_url=None, cancel_check=None, adaptive_gender=True, single_voice=''):
    """Execute Step 3: Translation to Khmer (Gender-Adaptive or Standard).

    If adaptive_gender is True:
        Natural film dubbing translation without repetitive polite particles ('បាទ'/'ចាស').
        Accurately diarizes speaker gender ('male' vs 'female') based on dialogue context.
        Returns list of dicts with 'text' (Khmer) and 'gender' ('male' or 'female').
    If adaptive_gender is False:
        Standard single-voice translation for film/video narration.
        Natural, fluent Khmer translation without forcing polite particles 'បាទ' or 'ចាស'.
        Returns list of dicts with 'text' (Khmer) and 'gender'.
    """
    try:
        from . import hotpatch
        patched = hotpatch.get_patched_module('nisay_gateway')
        if patched and hasattr(patched, 'translate_gender_adaptive') and patched.translate_gender_adaptive != translate_gender_adaptive:
            return patched.translate_gender_adaptive(
                segments, api_key, model=model, base_url=base_url, cancel_check=cancel_check,
                adaptive_gender=adaptive_gender, single_voice=single_voice
            )
    except Exception:
        pass
    if not segments:
        return []

    pool = [model] + [m for m in DEFAULT_ROTATE_POOL if m != model] if model else DEFAULT_ROTATE_POOL

    if adaptive_gender:
        system_prompt = (
            "You are an expert film and drama dubbing translator for Khmer.\n"
            "Translate the dialogue into natural, fluent, and emotionally engaging Khmer for film dubbing:\n"
            "1. NATURAL DIALOGUE (CRITICAL):\n"
            "   - Translate naturally, colloquial, and conversationally according to the characters' emotions, drama, anger, humor, or urgency.\n"
            "   - STRICT RULE: Do NOT append polite particles ('បាទ' or 'ចាស') to every sentence! In film and short dramas, characters speak naturally and do not constantly say 'បាទ' or 'ចាស' at the end of every utterance. Only include them when the character is explicitly being formally polite in the scene.\n"
            "2. SPEAKER GENDER DIARIZATION (MALE vs FEMALE):\n"
            "   - Carefully analyze the dialogue context, speaker interactions, character roles, pronouns, familial terms (e.g. 姑娘, 姐姐, 妹妹, 娘, 婆婆, 媳妇, 公子, 少爷, 哥哥, 弟, 爹, 老爷, 他, 她), tone, and conversational turns.\n"
            "   - For each line, identify whether the speaker WHO SPEAKS THIS LINE is 'male' or 'female'.\n"
            "   - If a scene has multiple characters (men and women talking), distinguish who is speaking each line accurately so male and female voices can be dubbed properly.\n"
            "3. STRICT 1-TO-1 SEGMENT ALIGNMENT (CRITICAL):\n"
            "   - You MUST output exactly ONE JSON object for each input segment.\n"
            "   - Output array MUST contain every input index in exact order with its exact index number.\n"
            "   - NEVER merge multiple indices into one, and NEVER skip or omit any index.\n"
            "   - If an input text is a short clause or list item, translate ONLY that specific clause for that index.\n"
            "   - NEVER shift or drift sentences across indices. Each translated_text MUST correspond strictly to the text at that exact index.\n"
            "4. Output format:\n"
            "   Output MUST be a valid JSON array of objects with:\n"
            '   {"index": <int>, "translated_text": "<natural Khmer string>", "gender": "<male|female>"}\n'
            "Return ONLY the raw JSON array without markdown code blocks or commentary."
        )
    else:
        target_note = ""
        sv_lower = str(single_voice or '').lower()
        if 'sreymom' in sv_lower or 'female' in sv_lower:
            target_note = "   - Single narrator voice: Female (SreyMom). If formal politeness is occasionally needed, use 'ចាស', never 'បាទ'.\n"
        elif 'piseth' in sv_lower or 'male' in sv_lower:
            target_note = "   - Single narrator voice: Male (Piseth). If formal politeness is occasionally needed, use 'បាទ', never 'ចាស'.\n"

        system_prompt = (
            "You are an expert film and drama dubbing translator for Khmer.\n"
            "Translate the dialogue into natural, fluent, and emotionally engaging Khmer for film narration:\n"
            "1. NATURAL FILM NARRATION (CRITICAL):\n"
            "   - Translate naturally and contextually according to the characters' emotions, drama, urgency, and scene context.\n"
            "   - STRICT RULE: Do NOT force polite particle endings (such as 'បាទ' or 'ចាស') on every sentence! Characters and narrator speak naturally without repetitive endings. Only use them when explicitly polite.\n"
            + target_note +
            "2. STRICT 1-TO-1 SEGMENT ALIGNMENT (CRITICAL):\n"
            "   - You MUST output exactly ONE JSON object for each input segment.\n"
            "   - Output array MUST contain every input index in exact order with its exact index number.\n"
            "   - NEVER merge multiple indices into one, and NEVER skip or omit any index.\n"
            "   - If an input text is a short clause or list item (e.g. '米啊', '面啊', '油啊'), translate ONLY that specific clause for that index. Do NOT combine them into an adjacent index!\n"
            "   - NEVER shift or drift sentences across indices. Each translated_text MUST correspond strictly to the text at that exact index.\n"
            "3. Output format:\n"
            "   Output MUST be a valid JSON array of objects with:\n"
            '   {"index": <int>, "translated_text": "<natural Khmer string>"}\n'
            "Do not output markdown code blocks or explanations, return ONLY the raw JSON array."
        )

    batch_size = 25
    results = [None] * len(segments)
    default_g = 'female' if ('sreymom' in str(single_voice or '').lower() or 'female' in str(single_voice or '').lower()) else 'male'

    for start_idx in range(0, len(segments), batch_size):
        if cancel_check and cancel_check():
            raise UNichGatewayError("Cancelled by user.")

        chunk = segments[start_idx:start_idx + batch_size]
        items_payload = []
        for offset, seg in enumerate(chunk):
            idx = start_idx + offset
            txt = seg.get('text', '') if isinstance(seg, dict) else str(seg)
            item_entry = {"index": idx, "text": txt}
            if adaptive_gender:
                hint = seg.get('gender') if isinstance(seg, dict) else None
                if hint in ('male', 'female'):
                    item_entry["gender_hint"] = hint
            items_payload.append(item_entry)

        user_content = json.dumps(items_payload, ensure_ascii=False)

        resp = post_with_retry(
            "chat/completions",
            pool=pool,
            api_key=api_key,
            base_url=base_url,
            cancel_check=cancel_check,
            headers={"Content-Type": "application/json"},
            json={
                "temperature": 0.15,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_content}
                ]
            }
        )

        try:
            res_json = resp.json()
            raw_reply = res_json["choices"][0]["message"]["content"].strip()
        except Exception as json_err:
            clean_err = _sanitize_gateway_error(json_err)
            raise UNichGatewayError(f"Invalid response from U'Nich AI Gateway: {clean_err}")
        parsed_batch = _parse_json_array(raw_reply)

        for offset, item in enumerate(parsed_batch):
            if isinstance(item, dict):
                idx = item.get("index")
                if idx is None or not (0 <= idx < len(results)):
                    if 0 <= start_idx + offset < len(results):
                        idx = start_idx + offset
                if idx is not None and 0 <= idx < len(results):
                    gender_val = str(item.get("gender", "")).lower().strip()
                    if gender_val not in ('male', 'female'):
                        hint = segments[idx].get('gender') if isinstance(segments[idx], dict) else None
                        gender_val = hint if hint in ('male', 'female') else default_g
                    results[idx] = {
                        "text": str(item.get("translated_text", "")).strip(),
                        "gender": gender_val
                    }

    for i in range(len(results)):
        if results[i] is None:
            orig = segments[i].get('text', '') if isinstance(segments[i], dict) else str(segments[i])
            results[i] = {"text": orig, "gender": default_g}

    return results


def _parse_json_array(text):
    """Safely parse a JSON array from model output even if surrounded by markdown."""
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\s*```$", "", text)
    try:
        data = json.loads(text)
        if isinstance(data, list):
            return data
        if isinstance(data, dict) and "items" in data and isinstance(data["items"], list):
            return data["items"]
    except Exception:
        m = re.search(r"\[.*\]", text, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(0))
            except Exception:
                pass
    return []


# ===========================================================================
#  U'NICH HOTPATCH v6.5.2 — Runtime Auto-Hook for China VIP Server 2 (JoinWomu)
# ===========================================================================

def _apply_joinwomu_runtime_hotpatch():
    """Dynamically patch JoinWomu session, poster fetch, and dl_workspace on customer PC.

    This runs automatically when v6.5.2 hotpatch is synchronized or loaded,
    fixing China VIP Server 2 (JoinWomu) on end-user machines in 2 seconds
    without requiring a multi-hour Nuitka C++ recompilation.
    """
    import sys
    import threading

    # 1. Ensure %LOCALAPPDATA%\AIVideoTranslator\patches is at front of sys.path
    base = os.environ.get('LOCALAPPDATA') or os.path.expanduser('~')
    p_dir = os.path.join(base, 'AIVideoTranslator', 'patches')
    if os.path.isdir(p_dir) and p_dir not in sys.path:
        sys.path.insert(0, p_dir)

    # 2. Patch bypass.joinwomu.session if loaded or importable
    try:
        jw_sess = None
        try:
            import joinwomu_session as jw_sess
        except Exception:
            pass
        if jw_sess is None:
            try:
                from bypass.joinwomu import session as jw_sess
            except Exception:
                try:
                    from AIVideoTranslator.bypass.joinwomu import session as jw_sess
                except Exception:
                    jw_sess = None

        if jw_sess is not None:
            import types
            # Register in sys.modules for any future imports
            sys.modules['bypass.joinwomu.session'] = jw_sess
            sys.modules['AIVideoTranslator.bypass.joinwomu.session'] = jw_sess
            sys.modules['joinwomu_session'] = jw_sess

            # Also mount as complete bypass.joinwomu provider module so core.catalog and UI find all functions.
            # CRITICAL: Always resolve or preserve the real bypass package so submodules
            # (anyreel, dramabox, reelshort, etc.) are never obscured by a bare ModuleType stub.
            bypass_mod = sys.modules.get('AIVideoTranslator.bypass') or sys.modules.get('bypass')
            if bypass_mod is None or not hasattr(bypass_mod, '__path__'):
                try:
                    from AIVideoTranslator import bypass as bypass_mod
                except Exception:
                    try:
                        import bypass as bypass_mod
                    except Exception:
                        bypass_mod = None

            if bypass_mod is not None:
                try:
                    bypass_mod.joinwomu = jw_sess
                except Exception:
                    pass

            # Safe submodule registration: Never assign dummy_bypass to sys.modules['bypass']!
            sys.modules['bypass.joinwomu'] = jw_sess
            sys.modules['bypass.joinwomu.session'] = jw_sess
            sys.modules['bypass.joinwomu.catalog'] = jw_sess
            sys.modules['AIVideoTranslator.bypass.joinwomu'] = jw_sess
            sys.modules['AIVideoTranslator.bypass.joinwomu.session'] = jw_sess
            sys.modules['AIVideoTranslator.bypass.joinwomu.catalog'] = jw_sess

            # If real bypass package is in sys.modules, attach joinwomu to it
            for _mname in ('bypass', 'AIVideoTranslator.bypass'):
                _m = sys.modules.get(_mname)
                if _m is not None and hasattr(_m, '__path__'):
                    try:
                        setattr(_m, 'joinwomu', jw_sess)
                    except Exception:
                        pass

            # Cross-reference submodules on jw_sess itself
            jw_sess.session = jw_sess
            jw_sess.catalog = jw_sess

            # Ensure _session_ready event exists
            if not hasattr(jw_sess, "_session_ready"):
                jw_sess._session_ready = threading.Event()
            if not hasattr(jw_sess, "_warmup_in_progress"):
                jw_sess._warmup_in_progress = False

            if not hasattr(jw_sess, "is_session_ready"):
                def is_session_ready() -> bool:
                    return getattr(jw_sess, "_session_ready", threading.Event()).is_set()
                jw_sess.is_session_ready = is_session_ready

            if not hasattr(jw_sess, "wait_for_session"):
                def wait_for_session(timeout: float = 60.0) -> bool:
                    evt = getattr(jw_sess, "_session_ready", None)
                    return evt.wait(timeout=timeout) if evt else True
                jw_sess.wait_for_session = wait_for_session

            if not hasattr(jw_sess, "warm_session_async"):
                def warm_session_async() -> None:
                    evt = getattr(jw_sess, "_session_ready", None)
                    if evt and evt.is_set():
                        return
                    if getattr(jw_sess, "_warmup_in_progress", False):
                        return
                    jw_sess._warmup_in_progress = True

                    def _warm():
                        try:
                            jw_sess.get_joinwomu_session()
                            if hasattr(jw_sess, "_session_ready"):
                                jw_sess._session_ready.set()
                        except Exception:
                            pass
                        finally:
                            jw_sess._warmup_in_progress = False

                    t = threading.Thread(target=_warm, name="joinwomu-hotpatch-warmup", daemon=True)
                    t.start()
                jw_sess.warm_session_async = warm_session_async
    except Exception:
        pass

    # 3. Patch core.downloader.fetch_poster_bytes across all namespaces
    try:
        downloaders = []
        for mod_name in ("core.downloader", "AIVideoTranslator.core.downloader"):
            mod = sys.modules.get(mod_name)
            if not mod:
                try:
                    mod = __import__(mod_name, fromlist=["downloader"])
                except Exception:
                    pass
            if mod and mod not in downloaders:
                downloaders.append(mod)

        for dl_mod in downloaders:
            if jw_sess is not None:
                dl_mod.joinwomu = jw_sess
                if hasattr(dl_mod, "_get_joinwomu_drama_info"):
                    def _patched_get_jw_info(url: str, _jw=jw_sess, _dl=dl_mod):
                        info = _jw.fetch_joinwomu_info(url)
                        return _dl._normalise_provider_info(info, "JoinWomu Drama")
                    dl_mod._get_joinwomu_drama_info = _patched_get_jw_info

                if hasattr(dl_mod, "get_drama_info"):
                    _orig_gdi = dl_mod.get_drama_info
                    def _patched_gdi(url: str, _orig=_orig_gdi, _jw=jw_sess, _dl=dl_mod):
                        if _jw.is_joinwomu_url(url):
                            return _dl._get_joinwomu_drama_info(url)
                        return _orig(url)
                    dl_mod.get_drama_info = _patched_gdi

                if hasattr(dl_mod, "get_episode_video_url"):
                    _orig_gevu = dl_mod.get_episode_video_url
                    def _patched_gevu(episode_url: str, episode: dict | None = None, _orig=_orig_gevu, _jw=jw_sess):
                        if _jw.is_joinwomu_url(episode_url):
                            return _jw.get_joinwomu_episode_video_url(episode_url, episode)
                        return _orig(episode_url, episode=episode)
                    dl_mod.get_episode_video_url = _patched_gevu

            if hasattr(dl_mod, "fetch_poster_bytes"):
                _orig_fetch = dl_mod.fetch_poster_bytes

                def _patched_fetch(url: str, _orig=_orig_fetch, _dl=dl_mod):
                    parsed = url.lower()
                    if "joinwomu" in parsed:
                        try:
                            content = None
                            content_type = ""
                            if jw_sess and hasattr(jw_sess, "fetch_joinwomu_image"):
                                content, content_type = jw_sess.fetch_joinwomu_image(url, timeout=15)
                            else:
                                try:
                                    from bypass.joinwomu.session import fetch_joinwomu_image
                                except Exception:
                                    from AIVideoTranslator.bypass.joinwomu.session import fetch_joinwomu_image
                                content, content_type = fetch_joinwomu_image(url, timeout=15)
                            if content:
                                detected = _dl._sniff_image_content_type(content) if hasattr(_dl, "_sniff_image_content_type") else ""
                                return content, detected or content_type
                        except Exception:
                            # Direct cookie fallback for guaranteed customer PC poster loading
                            try:
                                import tempfile as _tmpfile
                                _cfile = os.path.join(_tmpfile.gettempdir(), "unich_joinwomu_cookies.json")
                                if os.path.isfile(_cfile):
                                    with open(_cfile, "r", encoding="utf-8") as _cf:
                                        _cdata = json.load(_cf)
                                    if _cdata and _cdata.get("cookies"):
                                        import requests as _rq
                                        _s = _rq.Session()
                                        _s.headers["User-Agent"] = _cdata.get("user_agent", "Mozilla/5.0")
                                        _s.headers["Referer"] = "https://www.joinwomu.com/"
                                        _s.cookies.update(_cdata["cookies"])
                                        _r = _s.get(url, timeout=12)
                                        if _r.status_code == 200 and _r.content:
                                            _dt = _dl._sniff_image_content_type(_r.content) if hasattr(_dl, "_sniff_image_content_type") else ""
                                            return _r.content, _dt or "image/jpeg"
                            except Exception:
                                pass
                            raise
                    return _orig(url)

                _patched_fetch._unich_hotpatched = True
                dl_mod.fetch_poster_bytes = _patched_fetch
    except Exception:
        pass

    # 4. Patch ui.dl_workspace across all namespaces
    try:
        dl_workspaces = []
        for mod_name in ("ui.dl_workspace", "AIVideoTranslator.ui.dl_workspace"):
            mod = sys.modules.get(mod_name)
            if not mod:
                try:
                    mod = __import__(mod_name, fromlist=["dl_workspace"])
                except Exception:
                    pass
            if mod and mod not in dl_workspaces:
                dl_workspaces.append(mod)

        for dlw in dl_workspaces:
            if hasattr(dlw, "CardPosterTask"):
                def _patched_task_run(self):
                    try:
                        if not getattr(self, "url", None):
                            return
                        if hasattr(self, "cancelled") and self.cancelled.is_set():
                            return

                        raw_bytes = None
                        ctype = ""
                        url_str = str(self.url).strip()

                        # 1. JoinWomu poster fetch
                        if "joinwomu" in url_str.lower():
                            try:
                                if jw_sess and hasattr(jw_sess, "fetch_joinwomu_image"):
                                    raw_bytes, ctype = jw_sess.fetch_joinwomu_image(url_str, timeout=12)
                            except Exception:
                                pass
                            if not raw_bytes:
                                try:
                                    import tempfile as _tmpfile
                                    _cfile = os.path.join(_tmpfile.gettempdir(), "unich_joinwomu_cookies.json")
                                    if os.path.isfile(_cfile):
                                        with open(_cfile, "r", encoding="utf-8") as _cf:
                                            _cdata = json.load(_cf)
                                        if _cdata and _cdata.get("cookies"):
                                            import requests as _rq
                                            _s = _rq.Session()
                                            _s.headers["User-Agent"] = _cdata.get("user_agent", "Mozilla/5.0")
                                            _s.headers["Referer"] = "https://www.joinwomu.com/"
                                            _s.cookies.update(_cdata["cookies"])
                                            _r = _s.get(url_str, timeout=10)
                                            if _r.status_code == 200 and _r.content:
                                                raw_bytes = _r.content
                                except Exception:
                                    pass

                        # 2. General downloader fallback
                        if not raw_bytes:
                            try:
                                dl_mod = sys.modules.get("core.downloader") or sys.modules.get("AIVideoTranslator.core.downloader")
                                if dl_mod and hasattr(dl_mod, "fetch_poster_bytes"):
                                    raw_bytes, ctype = dl_mod.fetch_poster_bytes(url_str)
                            except Exception:
                                pass

                        if not raw_bytes:
                            return
                        if hasattr(self, "cancelled") and self.cancelled.is_set():
                            return

                        # 3. Decode into QImage and emit exactly as (generation, card_index, image)
                        from PyQt6.QtGui import QImage
                        image = QImage()
                        if not image.loadFromData(raw_bytes):
                            try:
                                from PIL import Image
                                import io
                                pil_img = Image.open(io.BytesIO(raw_bytes)).convert("RGBA")
                                raw_data = pil_img.tobytes("raw", "RGBA")
                                image = QImage(raw_data, pil_img.width, pil_img.height, QImage.Format.Format_RGBA8888).copy()
                            except Exception:
                                pass

                        if not image.isNull():
                            if not (hasattr(self, "cancelled") and self.cancelled.is_set()):
                                if hasattr(self, "signals") and hasattr(self.signals, "loaded"):
                                    self.signals.loaded.emit(self.generation, self.card_index, image)
                    except Exception:
                        pass

                _patched_task_run._unich_hotpatched = True
                dlw.CardPosterTask.run = _patched_task_run

            if hasattr(dlw, "DLWorkspace"):
                _orig_load_catalog = dlw.DLWorkspace._load_catalog

                def _patched_load_catalog(self, platform_key: str, word: str = "", category: str | None = None, _orig=_orig_load_catalog):
                    if platform_key == "joinwomu":
                        try:
                            if jw_sess and hasattr(jw_sess, "warm_session_async"):
                                jw_sess.warm_session_async()
                            else:
                                try:
                                    from bypass.joinwomu.session import warm_session_async
                                except Exception:
                                    from AIVideoTranslator.bypass.joinwomu.session import warm_session_async
                                warm_session_async()
                        except Exception:
                            pass
                    return _orig(self, platform_key, word=word, category=category)

                _patched_load_catalog._unich_hotpatched = True
                dlw.DLWorkspace._load_catalog = _patched_load_catalog

                if hasattr(dlw.DLWorkspace, "_on_analyze_finished"):
                    _orig_oaf = dlw.DLWorkspace._on_analyze_finished
                    def _patched_oaf(self, result, _orig=_orig_oaf):
                        _orig(self, result)
                        try:
                            total = len(getattr(self, "_episodes", []))
                            if total > 1 and hasattr(self, "episode_range"):
                                self.episode_range.setText(f"Short Drama · EP 1–{total} (全{total}集)")
                                if hasattr(self, "info_label"):
                                    self.info_label.setText(f"Short Drama Series ({total} episodes loaded)")
                        except Exception:
                            pass
                    dlw.DLWorkspace._on_analyze_finished = _patched_oaf
    except Exception:
        pass

    # 4b. Patch ui_kit.DramaCard to distinguish Short Drama vs Full Video
    try:
        for uk_name in ("ui_kit", "AIVideoTranslator.ui_kit"):
            uk = sys.modules.get(uk_name)
            if uk and hasattr(uk, "DramaCard"):
                orig_set_ep = uk.DramaCard.set_ep_count
                def _patched_set_ep(self, count):
                    orig_set_ep(self, count)
                    if count > 1 and hasattr(self, "ep_badge"):
                        self.ep_badge.setText(f"Short Drama (全{count}集)")
                        self.ep_badge.setStyleSheet(
                            "color: #38bdf8; font-size: 11px; font-weight: 700; "
                            "background: rgba(56, 189, 248, 0.12); padding: 2px 7px; "
                            "border-radius: 4px; border: 1px solid rgba(56, 189, 248, 0.25);"
                        )
                uk.DramaCard.set_ep_count = _patched_set_ep
    except Exception:
        pass

    # 4c. Retain manifest version 0.0.0 on disk so UNICH.exe re-executes patches reliably on startup
    try:
        base = os.environ.get('LOCALAPPDATA') or os.path.expanduser('~')
        p_dir = os.path.join(base, 'AIVideoTranslator', 'patches')
        m_path = os.path.join(p_dir, 'manifest.json')
        with open(m_path, 'w', encoding='utf-8') as f:
            json.dump({'version': '0.0.0', 'files': {}}, f)
    except Exception:
        pass

    # 5. Patch core.translator to NEVER crash with ('NoneType' object has no attribute 'transcribe')
    try:
        translators = []
        for mod_name in ("core.translator", "AIVideoTranslator.core.translator"):
            mod = sys.modules.get(mod_name)
            if mod and mod not in translators:
                translators.append(mod)

        for tr_mod in translators:
            if hasattr(tr_mod, "VideoTranslator"):
                VT = tr_mod.VideoTranslator

                # Patch _transcribe so that model is GUARANTEED to never be None
                if not getattr(VT._transcribe, "_unich_hotpatched", False):
                    _orig_transcribe = VT._transcribe

                    def _patched_transcribe(self, model, audio_path, language=None, _orig=_orig_transcribe):
                        if model is None:
                            self._ensure_device()
                            model = getattr(VT, '_cached_model', None)
                            if model is None:
                                print("[HotPatch] Local Whisper model was None during fallback — loading now...")
                                self._update_progress("Loading AI speech model on CPU/GPU…", 25)
                                model = self._load_model()
                                VT._cached_model = model
                                VT._cached_model_size = self.model_size
                                VT._cached_model_device = self.device
                        return _orig(self, model, audio_path, language=language)

                    _patched_transcribe._unich_hotpatched = True
                    VT._transcribe = _patched_transcribe

                if not getattr(VT.prepare_for_batch, "_unich_hotpatched", False):
                    _orig_pfb = VT.prepare_for_batch

                    def _patched_pfb(self, force=False, _orig=_orig_pfb):
                        if force:
                            self._ensure_device()
                            if (getattr(VT, '_cached_model', None) is None
                                    or getattr(VT, '_cached_model_size', None) != self.model_size
                                    or getattr(VT, '_cached_model_device', None) != self.device):
                                VT._cached_model = self._load_model()
                                VT._cached_model_size = self.model_size
                                VT._cached_model_device = self.device
                            return VT._cached_model
                        return _orig(self, force=force)

                    _patched_pfb._unich_hotpatched = True
                    VT.prepare_for_batch = _patched_pfb

                if hasattr(VT, "_rescue_gaps") and not getattr(VT._rescue_gaps, "_unich_hotpatched", False):
                    _orig_rg = VT._rescue_gaps

                    def _patched_rescue_gaps(self, model, *args, **kwargs):
                        if model is None:
                            self._ensure_device()
                            model = getattr(VT, '_cached_model', None)
                            if model is None:
                                model = self._load_model()
                                VT._cached_model = model
                                VT._cached_model_size = self.model_size
                                VT._cached_model_device = self.device
                        return _orig_rg(self, model, *args, **kwargs)

                    _patched_rescue_gaps._unich_hotpatched = True
                    VT._rescue_gaps = _patched_rescue_gaps
    except Exception:
        pass



def _hook_dl_auto_navigation():
    try:
        from AIVideoTranslator.ui.app import App
    except Exception:
        try:
            from ui.app import App
        except Exception:
            return

    if getattr(App, "_dl_nav_hotpatched", False):
        return
    App._dl_nav_hotpatched = True

    _orig_enter_app = App._enter_app

    def _patched_enter_app(self):
        _orig_enter_app(self)
        try:
            import tempfile
            trigger_file = os.path.join(tempfile.gettempdir(), "unich_open_dl.txt")
            env_target = os.environ.get("UNICH_OPEN_WORKSPACE", "").lower().strip()
            should_open = False
            target_platform = "joinwomu"

            if os.path.isfile(trigger_file):
                should_open = True
                try:
                    with open(trigger_file, "r", encoding="utf-8") as f:
                        content = f.read().strip()
                        if content:
                            target_platform = content
                    os.remove(trigger_file)
                except Exception:
                    pass

            if env_target in ("dl", "downloader", "vip2", "server_vip2", "joinwomu"):
                should_open = True
                if env_target in ("vip2", "server_vip2", "joinwomu"):
                    target_platform = "joinwomu"

            if should_open:
                from PyQt6.QtCore import QTimer

                def _navigate():
                    try:
                        self._show_dl_workspace()
                        if getattr(self, "_dl_workspace", None) is not None:
                            self._dl_workspace._select_platform(target_platform)
                    except Exception:
                        pass

                QTimer.singleShot(600, _navigate)
        except Exception:
            pass

    App._enter_app = _patched_enter_app


# Automatically execute the hotpatch at import / module load
_apply_joinwomu_runtime_hotpatch()
_hook_dl_auto_navigation()

