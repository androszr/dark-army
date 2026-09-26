"""Area contract: closed values, stable ordered leads, no capabilities."""
import pytest
from dark_army_daemon import areas, identity

@pytest.mark.parametrize('area', areas.AREAS, ids=lambda a: a.slug)
def test_area_table_and_walk(area):
    assert areas.get(area.slug) == area
    assert areas.name(area.slug) == area.name
    assert areas.pool_for(area.slug) == area.pool
    assert areas.anchor(area.slug) == area.pool[0]
    assert areas.allocate(area.slug, 'one', set()) == area.pool[0]
    assert areas.allocate(area.slug, 'two', {area.pool[0]}) == area.pool[1]
    busy = set(area.pool)
    assert areas.allocate(area.slug, 'one', busy) in busy
    assert areas.allocate(area.slug, 'one', busy) == areas.allocate(area.slug, 'one', busy)
    assert areas.brief_path(area.slug) == f'.claude/leads/{area.slug}.md'
    assert not busy.intersection(identity.ART_ONLY)
    assert areas.CHIEF_OF_STAFF not in busy

@pytest.mark.parametrize('value,want', [('Backbone','backbone'),(' DESK ','desk'),('pocket','pocket'),('Ledger','ledger'),('PLAY','play'),('Conductor','conductor'),('gate','gate'),('Universal','universal'),('', ''),('NONE',''),(None,'')])
def test_normalise(value,want):
    assert areas.normalise(value) == (want,'')

@pytest.mark.parametrize('value', ['weird','backbone, desk','../desk',42])
def test_normalise_refuses(value):
    assert areas.normalise(value) == ('',areas.AREA_REFUSAL)

def test_empty_means_universal_for_allocation_only():
    assert areas.get('') == areas.get('universal')
    assert areas.allocate('', 'c', set()) == 'proxy'
    assert areas.brief_path('') == ''
    assert areas.lead_line('Proxy','') == ''

def test_unknown():
    assert areas.get('unknown') is None
    assert areas.pool_for('unknown') == ()
    assert areas.anchor('unknown') == ''
    assert areas.allocate('unknown','c',set()) == ''
    assert areas.brief_path('unknown') == ''

def test_lead_lines():
    assert areas.lead_line('Relay','backbone') == 'Relay · Backbone lead'
    assert areas.lead_line('Ptys-abcd','pocket') == 'Ptys-abcd · Pocket lead'
    assert areas.lead_line('Vex','backbone') == 'Vex · Backbone stand-in'

def test_order():
    assert areas.slugs() == ('backbone','desk','pocket','ledger','play','conductor','gate','universal')


def test_area_and_character_slugs_meet_only_at_ledger():
    """Since 22 Sep 2026 `ledger` is both an area slug and a character slug.
    Area lookups take area slugs and character lookups take nicknames, so
    the shared word is harmless — this pins that it is the only one and that
    each side still reads it as its own kind."""
    assert {a.slug for a in areas.AREAS} & {n.lower() for n in identity.NAMES} == {"ledger"}
    assert areas.normalise("Ledger") == ("ledger", "")
    assert areas.pool_for("ledger") == ("audit", "ledger")
    assert areas.lead_line("Ledger", "ledger") == "Ledger · Ledger lead"
    assert areas.lead_line("Audit", "ledger") == "Audit · Ledger lead"
