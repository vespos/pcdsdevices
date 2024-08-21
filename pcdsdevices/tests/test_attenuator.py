import logging
import threading
import time
from unittest.mock import Mock

import pytest
from ophyd.sim import make_fake_device
from ophyd.status import wait as status_wait

from ..attenuator import (AT1K2, AT1K4, AT2K2, AT2L0, MAX_FILTERS, AttBase,
                          Attenuator, _att_classes)

logger = logging.getLogger(__name__)


# Replace all the Attenuator classes with fake classes
for name, cls in _att_classes.items():
    _att_classes[name] = make_fake_device(cls)


@pytest.mark.timeout(5)
def test_attenuator_states(fake_att):
    logger.debug('test_attenuator_states')
    att = fake_att
    # Set no transmission
    att.readback.sim_put(0)
    assert not att.removed
    assert att.inserted
    # Set full transmission
    att.readback.sim_put(1)
    assert att.removed
    assert not att.inserted


def test_attenuator_bluesky(fake_att):
    logger.debug('test_attenuator_bluesky')
    fake_att.read()
    fake_att.describe()


def fake_move_transition(att, status, goal):
    """
    Set to the PVs sort of like it would happen in the real world and check the
    status
    """
    # Set status to "MOVING"
    att.done.sim_put(1)
    # Set transmission to the goal
    att.readback.sim_put(goal)
    # Set status to "OK"
    att.done.sim_put(0)
    # Check that the object responded properly
    status_wait(status, timeout=1)
    assert status.done
    assert status.success


@pytest.mark.timeout(5)
def test_attenuator_motion(fake_att):
    logger.debug('test_attenuator_motion')
    att = fake_att
    # Set up the ceil and floor
    att.trans_ceil.sim_put(0.8001)
    att.trans_floor.sim_put(0.5001)
    # Move to ceil
    status = att.move(0.8, wait=False)
    fake_move_transition(att, status, 0.8001)
    assert att.setpoint.get() == 0.8
    assert att.actuate_value == 3
    # Move to floor
    status = att.move(0.5, wait=False)
    fake_move_transition(att, status, 0.5001)
    assert att.setpoint.get() == 0.5
    assert att.actuate_value == 2
    # Call remove method
    status = att.remove(wait=False)
    fake_move_transition(att, status, 1)
    assert att.setpoint.get() == 1
    # Call insert method
    status = att.insert(wait=False)
    fake_move_transition(att, status, 0)
    assert att.setpoint.get() == 0


@pytest.mark.timeout(5)
def test_attenuator_no_interrupt(fake_att):
    logger.debug('test_attenuator_no_interrupt')
    att = fake_att
    # Set as already moving
    att.done.sim_put(1)
    with pytest.raises(RuntimeError):
        att.move(0.5)


@pytest.mark.timeout(5)
def test_attenuator_subscriptions(fake_att):
    logger.debug('test_attenuator_subscriptions')
    att = fake_att
    cb = Mock()
    att.subscribe(cb, run=False)
    att.readback.sim_put(0.5)
    assert cb.called
    state_cb = Mock()
    att.subscribe(state_cb, event_type=att.SUB_STATE, run=False)
    att.readback.sim_put(0.6)
    assert not state_cb.called
    att.done.sim_put(1)
    att.done.sim_put(0)
    assert state_cb.called


@pytest.mark.timeout(5)
def test_attenuator_calcpend(fake_att):
    logger.debug('test_attenuator_calcpend')
    att = fake_att
    att.calcpend.sim_put(1)
    # Initialize to any value
    att.setpoint.sim_put(1)
    att.trans_ceil.sim_put(1)
    att.trans_floor.sim_put(1)

    def wait_put(sig, val, delay):
        time.sleep(delay)
        sig.sim_put(val)
    t = threading.Thread(target=wait_put, args=(att.calcpend, 0, 0.4))
    t.start()
    start = time.time()
    # Waits for calcpend to be 0
    att.actuate_value
    assert 0.1 < time.time() - start < 1
    att.calcpend.sim_put(1)
    # Gives up after one second
    start = time.time()
    att.actuate_value
    assert time.time() - start >= 1


@pytest.mark.timeout(5)
def test_attenuator_set_energy(fake_att):
    logger.debug('test_attenuator_set_energy')
    att = fake_att
    att.set_energy()
    assert att.eget_cmd.get() == 6
    energy = 1000
    att.set_energy(energy)
    assert att.eget_cmd.get() == 0
    assert att.user_energy.get() == energy


def test_attenuator_transmission(fake_att):
    logger.debug('test_attenuator_transmission')
    att = fake_att
    assert att.transmission == att.position


@pytest.mark.timeout(5)
def test_attenuator_staging(fake_att):
    logger.debug('test_attenuator_staging')
    att = fake_att
    # Set up at least one invalid state
    att.filter1.state.sim_put(att.filter1._unknown)
    att.stage()
    for filt in att.filters:
        filt.insert(wait=True)
    att.unstage()
    for filt in att.filters:
        assert filt.removed


def test_attenuator():
    logger.debug('test_attenuator')
    att = Attenuator('TRD:ATT', MAX_FILTERS-1, name='att')
    att.wait_for_connection()


@pytest.mark.timeout(5)
def test_attenuator_disconnected():
    AttBase('TST:ATT', name='test_att')


@pytest.fixture(
    params=['at2l0', 'at1k4', 'at1k2', 'at2k2']
)
def fake_new_attenuator(request):
    """Attenuators new to LCLS-II."""
    attname = request.param
    if attname == 'at2l0':
        FakeAT2L0 = make_fake_device(AT2L0)
        return FakeAT2L0('AT2L0:', name='fake_at2l0')
    if attname == 'at1k4':
        FakeAT1K4 = make_fake_device(AT1K4)
        return FakeAT1K4('AT1K4:', calculator_prefix='AT1K4:CALC',
                         name='fake_at1k4')
    if attname == 'at1k2':
        FakeAT1K2 = make_fake_device(AT1K2)
        return FakeAT1K2('AT1K2:', calculator_prefix='AT1K2:CALC',
                         name='fake_at1k2')
    if attname == 'at2k2':
        FakeAT2K2 = make_fake_device(AT2K2)
        return FakeAT2K2('AT2K2:', calculator_prefix='AT2K2:CALC',
                         name='fake_at2k2')
    raise RuntimeError(f'Unknown attenuator {attname}')


def test_new_attenuator_smoke(fake_new_attenuator):
    fake_new_attenuator.setpoint
    fake_new_attenuator.readback
    fake_new_attenuator.actuate
    fake_new_attenuator(0.0)
    fake_new_attenuator(1.0)
    fake_new_attenuator.wm()
    with pytest.raises(ValueError):
        fake_new_attenuator(1.1)
    print(
        fake_new_attenuator.format_status_info(
            fake_new_attenuator.status_info()
        )
    )


@pytest.fixture(scope="function")
def at2l0(request):
    at2l0 = make_fake_device(AT2L0)("AT2L0:", name=f"fake_at2l0_for_{request.function}")
    for walk in at2l0.walk_signals():
        try:
            if "message" in walk.dotted_name:
                walk.item.sim_put("")
            else:
                walk.item.sim_put(0)
        except AttributeError:
            ...
        walk.item._metadata["severity"] = 0
    return at2l0


def test_at2l0_error_summary(at2l0):
    assert at2l0.error_summary.get() == "No Errors"
    at2l0.blade_05.state.error_message.sim_put("test error")
    assert "test error" in at2l0.error_summary.get()


def test_at2l0_error_bitmask(at2l0):
    assert at2l0.error_summary_bitmask.get() == 0
    # blade_01 intentionally ignored (mirror)
    at2l0.blade_01.motor.plc.err_code.sim_put(1)
    assert at2l0.error_summary_bitmask.get() == 0
    at2l0.blade_19.motor.plc.err_code.sim_put(1)
    # bitmask intentionally reversed (better for ui)
    assert at2l0.error_summary_bitmask.get() == 0b1
    at2l0.blade_17.motor.plc.err_code.sim_put(1)
    assert at2l0.error_summary_bitmask.get() == 0b101


def test_at2l0_clear_errors(at2l0):
    signals = []
    for blade_num in range(1, 20):
        blade_obj = getattr(at2l0, f"blade_{blade_num:02}")
        signals.append(blade_obj.motor.plc.cmd_err_reset)
        signals.append(blade_obj.state.reset_cmd)
    for sig in signals:
        assert not sig.get()
    at2l0.clear_errors()
    for sig in signals:
        assert sig.get()
