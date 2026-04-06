from __future__ import annotations

import subprocess


# MARK: - ALSA mixer helpers

_saved_input_sources: dict = {}


def _amixer_find_input_source_numids(alsa_card: int) -> list[int]:
    """
    Return a list of numids for 'Input Source' controls on the given card,
    one per capture channel, in index order.  Returns [] if amixer is not found.
    """
    try:
        result = subprocess.run(
            ['amixer', '-c', str(alsa_card), 'controls'],
            capture_output=True, text=True,
        )
    except FileNotFoundError:
        return []
    numids = []
    for line in result.stdout.splitlines():
        if "name='Input Source'" in line:
            for part in line.split(','):
                if part.startswith('numid='):
                    numids.append(int(part.split('=')[1]))
    return sorted(numids)


def _amixer_cset_numid(alsa_card: int, numid: int, value: str) -> None:
    try:
        subprocess.run(
            ['amixer', '-c', str(alsa_card), 'cset', f'numid={numid}', value],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except FileNotFoundError:
        pass


def _amixer_cget_numid(alsa_card: int, numid: int) -> str | None:
    """Return the current enum item name for the given numid."""
    try:
        result = subprocess.run(
            ['amixer', '-c', str(alsa_card), 'cget', f'numid={numid}'],
            capture_output=True, text=True,
        )
    except FileNotFoundError:
        return None
    items = {}
    current_index = None
    for line in result.stdout.splitlines():
        line = line.strip()
        m_item = line.startswith('; Item #')
        if m_item:
            idx   = int(line.split('#')[1].split(' ')[0])
            label = line.split("'")[1]
            items[idx] = label
        elif line.startswith(': values='):
            current_index = int(line.split('=')[1])
    if current_index is not None:
        return items.get(current_index)
    return None


def switch_alsa_input_to_line_in(alsa_card: int = 0) -> None:
    """Save current ALSA Input Source settings, then switch all channels to Line."""
    numids = _amixer_find_input_source_numids(alsa_card)
    for i, numid in enumerate(numids):
        original = _amixer_cget_numid(alsa_card, numid)
        if original:
            _saved_input_sources[i] = (numid, original)
        _amixer_cset_numid(alsa_card, numid, 'Line')


def restore_alsa_input_sources(alsa_card: int = 0) -> None:
    """Restore ALSA Input Source settings saved before we changed them."""
    fallback_defaults = ['Rear Mic', 'Front Mic']
    numids = _amixer_find_input_source_numids(alsa_card)
    for i, numid in enumerate(numids):
        if i in _saved_input_sources:
            _, value = _saved_input_sources[i]
        else:
            value = fallback_defaults[i] if i < len(fallback_defaults) else 'Rear Mic'
        _amixer_cset_numid(alsa_card, numid, value)
