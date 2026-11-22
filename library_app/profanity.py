"""Profanity / spam filter for member-written text (book review comments).

Design notes
------------
* Comments that fail the check are REJECTED (the member is asked to reword); we never
  silently edit what someone wrote.
* Text is normalised first so common evasions are caught: accents, upper/lower case,
  leetspeak (f*ck, sh1t, @ss), stretched letters (fuuuck), separators (f.u.c.k) and
  spaced-out letters (f u c k).
* Matching is token based, so innocent words that merely contain a bad word
  (Scunthorpe, assassin, class, Hancock) are not flagged. A short list of very
  unambiguous words is also matched inside longer tokens (e.g. "motherfucker").
* No word list is perfect. Admins/librarians can still delete any review.
"""
import re
import unicodedata

# Whole-word matches (after normalisation). English + Filipino/Tagalog.
BLOCKED_WORDS = {
    # English
    'fuck', 'fucker', 'fucking', 'fucked', 'fuckin', 'fck', 'fcking', 'fckin', 'fcker', 'fcked',
    'fuk', 'fuc', 'fking', 'fkin', 'sht', 'shyt', 'btch', 'shit', 'shitty', 'bullshit',
    'bitch', 'bastard', 'asshole', 'ass', 'arse', 'dick', 'cock', 'pussy', 'cunt', 'whore',
    'slut', 'twat', 'wanker', 'prick', 'dumbass', 'jackass', 'douche', 'douchebag',
    'nigger', 'nigga', 'faggot', 'fag', 'retard', 'retarded', 'dyke', 'tranny', 'chink',
    'spic', 'kike', 'piss', 'crap', 'damn', 'goddamn', 'bollocks', 'motherfucker',
    # Filipino / Tagalog
    'puta', 'putang', 'putangina', 'putanginamo', 'tangina', 'tanginamo', 'tangna', 'potangina',
    'gago', 'gaga', 'tarantado', 'ulol', 'bobo', 'tanga', 'inutil', 'lintik', 'punyeta',
    'pakshet', 'pakyu', 'pucha', 'puchang', 'bwisit', 'buwisit', 'hinayupak',
    'kantot', 'kantutan', 'iyot', 'jakol', 'tamod', 'burat', 'titi', 'puki', 'pekpek', 'pokpok',
    'bayag', 'pota', 'shet', 'yawa',
}

# Unambiguous stems that are also blocked when buried inside a longer token.
# Kept deliberately short so we do not flag normal words.
BLOCKED_SUBSTRINGS = {
    'fuck', 'shit', 'bitch', 'cunt', 'nigger', 'nigga', 'faggot', 'asshole',
    'putangina', 'tangina', 'putang', 'pakyu', 'pakshet',
}

# Multi-word phrases (matched on the normalised, space separated text).
BLOCKED_PHRASES = {
    'tang ina', 'putang ina', 'puta ka', 'gago ka', 'tangina mo', 'putang ina mo', 'ina mo',
    'pak yu', 'pak shet', 'son of a bitch', 'piece of shit', 'go to hell', 'kill yourself',
    'kys', 'shut the fuck up', 'eat shit', 'bobo ka', 'tanga ka', 'ulol ka',
}

# Tokens that contain a blocked substring but are innocent.
ALLOWED_WORDS = {
    'scunthorpe', 'shitake', 'shiitake', 'assassin', 'assassinate', 'class', 'classic',
    'classical', 'pass', 'passage', 'grass', 'glass', 'mass', 'bass', 'brass', 'assume',
    'assist', 'assess', 'asset', 'assign', 'hancock', 'cocktail', 'peacock', 'dickens',
    'dickinson', 'analysis', 'amass', 'harass', 'embarrass', 'compass', 'passion', 'passenger',
    'title', 'titles', 'titled', 'petite', 'animal', 'animals', 'animation', 'animated',
    'damnation', 'tangled', 'tango', 'tangible', 'tangent', 'putty', 'leche',
}
# Exact matches against BLOCKED_WORDS are always checked first, so this list can only
# rescue longer words that merely CONTAIN a blocked stem.

LEET_MAP = str.maketrans({
    '0': 'o', '1': 'i', '3': 'e', '4': 'a', '5': 's', '7': 't', '8': 'b',
    '@': 'a', '$': 's', '!': 'i', '|': 'i', '+': 't', '€': 'e', '¢': 'c',
})

URL_RE = re.compile(r'(https?://|www\.|\b[a-z0-9-]+\.(com|net|org|ph|io|co|me|xyz|info|ru|cn)\b)', re.I)
EMAIL_RE = re.compile(r'[\w.+-]+@[\w-]+\.[\w.-]+')
PHONE_RE = re.compile(r'(?:\+?63|0)?9\d{2}[\s.-]?\d{3}[\s.-]?\d{4}|\b\d{3}[\s.-]\d{3,4}[\s.-]\d{4}\b')


def _strip_accents(text):
    return ''.join(c for c in unicodedata.normalize('NFKD', text) if not unicodedata.combining(c))


def _collapse_repeats(word):
    """'fuuuuck' -> 'fuck', 'shiit' -> 'shit' (runs of the same letter become one)."""
    return re.sub(r'(.)\1+', r'\1', word)


def _normalise(text):
    text = _strip_accents(text).lower().translate(LEET_MAP)
    text = re.sub(r'[^a-z\s]', ' ', text)  # punctuation/digits left over become separators
    return re.sub(r'\s+', ' ', text).strip()


def _tokens(text):
    """Tokens from the raw text, where punctuation INSIDE a word is removed (f.u.c.k, f*ck)
    and spaced-out single letters are glued together (f u c k)."""
    lowered = _strip_accents(text).lower().translate(LEET_MAP)
    # glue things like "f.u.c.k" / "f-u-c-k" / "f_u_c_k" / "f*ck"
    lowered = re.sub(r'(?<=[a-z])[\.\-_\*\u00b7](?=[a-z])', '', lowered)
    raw = re.findall(r'[a-z]+', lowered)
    glued, run = [], []
    for tok in raw:
        if len(tok) == 1:
            run.append(tok)
            continue
        if run:
            glued.append(''.join(run))
            run = []
        glued.append(tok)
    if run:
        glued.append(''.join(run))
    return glued


_COLLAPSED_WORDS = {_collapse_repeats(w) for w in BLOCKED_WORDS}
_COLLAPSED_SUBSTRINGS = {_collapse_repeats(w) for w in BLOCKED_SUBSTRINGS}
_COLLAPSED_ALLOWED = {_collapse_repeats(w) for w in ALLOWED_WORDS} - _COLLAPSED_WORDS
_COLLAPSED_PHRASES = {' '.join(_collapse_repeats(w) for w in ph.split()): ph for ph in BLOCKED_PHRASES}


def _is_blocked_token(token):
    if token in BLOCKED_WORDS:
        return True
    collapsed = _collapse_repeats(token)
    if collapsed in _COLLAPSED_WORDS:
        return True
    if token in ALLOWED_WORDS or collapsed in _COLLAPSED_ALLOWED:
        return False
    return any(stem in collapsed for stem in _COLLAPSED_SUBSTRINGS)


def find_profanity(text):
    """Return a sorted list of the blocked terms found in `text` (empty list = clean)."""
    if not text:
        return []
    found = {t for t in _tokens(text) if _is_blocked_token(t)}
    phrase_text = ' '.join(_collapse_repeats(w) for w in _normalise(text).split())
    for collapsed, original in _COLLAPSED_PHRASES.items():
        if re.search(r'\b' + re.escape(collapsed) + r'\b', phrase_text):
            found.add(original)
    return sorted(found)


def contains_profanity(text):
    return bool(find_profanity(text))


def find_contact_or_link(text):
    """True if the text contains a link, e-mail address or phone number (spam / privacy)."""
    return bool(text and (URL_RE.search(text) or EMAIL_RE.search(text) or PHONE_RE.search(text)))
