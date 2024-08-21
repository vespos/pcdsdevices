"""
Module for `Attenuator` and related classes.
"""
import enum
import functools
import logging
import time
from typing import Generator

import numpy as np
import prettytable
from lightpath import LightpathState
from ophyd.device import Component as Cpt
from ophyd.device import Device
from ophyd.device import DynamicDeviceComponent as DDC
from ophyd.device import FormattedComponent as FCpt
from ophyd.pv_positioner import PVPositionerPC
from ophyd.signal import EpicsSignal, EpicsSignalRO, Signal, SignalRO

from . import utils
from .analog_signals import FDQ
from .device import GroupDevice
from .device import UnrelatedComponent as UCpt
from .device import UpdateComponent as UpCpt
from .epics_motor import BeckhoffAxisNoOffset
from .inout import InOutPositioner, TwinCATInOutPositioner
from .interface import (BaseInterface, FltMvInterface, LightpathInOutCptMixin,
                        LightpathMixin)
from .pmps import TwinCATStatePMPS
from .pv_positioner import PVPositionerNoInterrupt
from .signal import InternalSignal, MultiDerivedSignal, MultiDerivedSignalRO
from .type_hints import OphydDataType, SignalToValue
from .utils import get_status_float, get_status_value
from .valve import VCN, VVC
from .variety import set_metadata

logger = logging.getLogger(__name__)
MAX_FILTERS = 12


class GattApertureY(TwinCATStatePMPS):
    """
    AT1K0 is a gas attenuator containing 4 discrete aperture arrays,
    each with 4 discrete aperture sizes to attenuate the intensity of the beam.
    This class Controls the Y axis component of the apertures inside AT1K0 and
    Defines the state count as 4, (5.5mm, 8mm, 10mm, 13mm) to limit the
    number of config PVs we connect to.
    """

    in_states = []
    out_states = []
    config = UpCpt(state_count=4)


class GattApertureX(TwinCATStatePMPS):
    """
    AT1K0 is a gas attenuator containing 4 discrete aperture arrays,
    each with 4 discrete aperture sizes to attenuate the intensity of the beam.
    Controls the X axis component of the apertures inside AT1K0
    Defines the state count as 1, (centered) to limit the number of
    config PVs we connect to.
    """

    in_states = []
    out_states = []
    config = UpCpt(state_count=1)


class Filter(InOutPositioner):
    """
    A single attenuation blade.

    Each of these has it's own in/out state, thickness, and material that are
    used in the attenuator IOC's calculations. It also has the capability to
    mark itself as 'STUCK IN' or 'STUCK OUT' so the transmission calculator
    can work around mechanical problems.

    This is not intended to be instantiated by a user, but instead included as
    a :class:`~ophyd.device.Component` in a subclass of :class:`AttBase`. You
    can instantiate these classes via the :func:`Attenuator` factory function.
    """

    status = Cpt(InternalSignal, kind='normal')
    state = Cpt(EpicsSignal, ':STATE', write_pv=':GO', kind='normal')
    stuck = Cpt(EpicsSignal, ':IS_STUCK', kind='normal')
    thickness = Cpt(EpicsSignal, ':THICK', kind='config')
    material = Cpt(EpicsSignal, ':MATERIAL', kind='config')

    tab_component_names = True

    def __init__(self, prefix, *, name, **kwargs):
        self._status_state = None
        self._stuck_state = None
        super().__init__(prefix, name=name, **kwargs)

    @state.sub_value
    def _state_update(self, *args, value, **kwargs):
        self._status_state = value
        self._status_update()

    @stuck.sub_value
    def _stuck_update(self, *args, value, **kwargs):
        self._stuck_state = value
        self._status_update()

    def _status_update(self):
        if self._stuck_state == 1:
            self.status.put(BladeStateEnum.STUCK_IN, force=True)
        elif self._stuck_state == 2:
            self.status.put(BladeStateEnum.STUCK_OUT, force=True)
        elif self._status_state == 1:
            self.status.put(BladeStateEnum.IN, force=True)
        elif self._status_state == 2:
            self.status.put(BladeStateEnum.OUT, force=True)
        else:
            self.status.put(BladeStateEnum.Unknown, force=True)


class FeeFilter(InOutPositioner):
    """A single attenuation blade, as implemented in the FEE."""

    status = Cpt(InternalSignal, kind='normal')
    state = Cpt(EpicsSignal, ':STATE', write_pv=':CMD')

    states_list = ['IN', 'OUT', 'FAIL']
    _invalid_states = ['FAIL']
    _unknown = 'XSTN'

    @state.sub_value
    def _status_update(self, *args, value, **kwargs):
        if value == 1:
            self.status.put(BladeStateEnum.IN, force=True)
        elif value == 2:
            self.status.put(BladeStateEnum.OUT, force=True)
        else:
            self.status.put(BladeStateEnum.Unknown, force=True)


class AttBase(FltMvInterface, PVPositionerNoInterrupt):
    """
    Base class for pre-L2SI beam power attenuators.

    This is a device that puts an array of filters in or out to achieve a
    desired transmission ratio.

    This class does not include filters, because the number of filters can
    vary. You should not instantiate this class directly, but instead use the
    :func:`Attenuator` factory function.
    """
    # fundamental frequency components
    # Positioner Signals
    setpoint = Cpt(EpicsSignal, ':COM:R_DES', auto_monitor=True,
                   kind='normal')
    readback = Cpt(EpicsSignalRO, ':COM:R_CUR', auto_monitor=True,
                   kind='hinted')
    actuate = Cpt(EpicsSignal, ':COM:GO', kind='omitted')
    done = Cpt(EpicsSignalRO, ':COM:STATUS', auto_monitor=True,
               kind='omitted')

    # Attenuator Signals
    energy = Cpt(EpicsSignalRO, ':COM:T_CALC.VALE', kind='normal')
    trans_ceil = Cpt(EpicsSignalRO, ':COM:R_CEIL', kind='omitted')
    trans_floor = Cpt(EpicsSignalRO, ':COM:R_FLOOR', kind='omitted')
    user_energy = Cpt(EpicsSignal, ':COM:EDES', kind='omitted')
    eget_cmd = Cpt(EpicsSignal, ':COM:EACT.SCAN', kind='omitted')

    # Aux Signals
    calcpend = Cpt(EpicsSignalRO, ':COM:CALCP', kind='omitted')

    egu = ''  # Transmission is a unitless ratio
    done_value = 0

    # QIcon for UX
    _icon = 'fa.barcode'
    # Subscription Types
    SUB_STATE = 'state'
    # Tab complete whitelist
    tab_whitelist = ['set_energy']

    def __init__(self, prefix, *, name, **kwargs):
        super().__init__(prefix, name=name, limits=(0, 1), **kwargs)
        self.filters = []
        self._has_subscribed_state = False
        for i in range(1, MAX_FILTERS + 1):
            try:
                self.filters.append(getattr(self, f'filter{i}'))
            except AttributeError:
                break

    @property
    def actuate_value(self):
        """
        Sets the value we use in the 'GO' command.

        This command will return 3 if the setpoint is closer to the ceiling
        than the floor, or 2 otherwise. In the unlikely event of a tie, we
        choose the floor.

        This will wait until a pending calculation completes before returning.
        """

        timeout = 1
        start = time.time()
        while self.calcpend.get() != 0:
            if time.time() - start > timeout:
                break
            time.sleep(0.01)

        goal = self.setpoint.get()
        ceil = self.trans_ceil.get()
        floor = self.trans_floor.get()

        if abs(goal - ceil) > abs(goal - floor):
            return 2
        else:
            return 3

    def set_energy(self, energy=None):
        """
        Sets the energy to use for transmission calculations.

        Parameters
        ----------
        energy : number, optional
            If provided, this is the energy we'll use for the transmission
            calcluations. If omitted, we'll clear any set energy and use the
            current beam energy instead.
        """

        if energy is None:
            logger.debug('Setting %s to use live energy', self.name or self)
            self.eget_cmd.put(6)
        else:
            logger.debug('Setting %s to use energy=%s',
                         self.name, energy)
            self.eget_cmd.put(0, use_complete=True)
            self.user_energy.put(energy)

    @property
    def transmission(self):
        """
        Ratio of pass-through beam to incoming beam as a value between
        1 (full beam) and 0 (no beam).
        """
        return self.position

    @property
    def inserted(self):
        """`True` if any blade is inserted."""
        return self.position < 1

    @property
    def removed(self):
        """`True` if all blades are removed."""
        return self.position == 1

    def insert(self, wait=False, timeout=None, moved_cb=None):
        """Block the beam by setting transmission to zero."""
        return self.move(0, wait=wait, timeout=timeout, moved_cb=moved_cb)

    def remove(self, wait=False, timeout=None, moved_cb=None):
        """Bring the attenuator fully out of the beam."""
        return self.move(1, wait=wait, timeout=timeout, moved_cb=moved_cb)

    def stage(self):
        """
        Store the original positions of all filter blades.

        This is a ``bluesky`` method called to set up the device for a scan.
        At the end of the scan, :meth:`.unstage` should be called to restore
        the original positions of the filter blades.

        This is better then storing and restoring the transmission because the
        mechanical state associated with a particular transmission changes with
        the beam energy.
        """

        for filt in self.filters:
            # If state is invalid, try to remove at end
            if filt.position in filt._invalid_states:
                self._original_vals[filt.state] = filt.out_states[0]
            # Otherwise, remember so we can restore
            else:
                self._original_vals[filt.state] = filt.state.get()
        return super().stage()

    def _setup_move(self, position):
        """
        If we're at a destination, short-circuit the done.

        This was needed because the status PV in the attenuator IOC does not
        react if we request a move to a transmission we've already reached.
        Therefore, this prevents a pointless timeout.
        """

        old_position = self.position
        super()._setup_move(position)
        ceil = self.trans_ceil.get()
        floor = self.trans_floor.get()
        if any(np.isclose((old_position, old_position), (ceil, floor))):
            moving_val = 1 - self.done_value
            self._move_changed(value=moving_val)
            self._move_changed(value=self.done_value)

    def subscribe(self, cb, event_type=None, run=True):
        cid = super().subscribe(cb, event_type=event_type, run=run)
        if event_type is None:
            event_type = self._default_sub
        if event_type == self.SUB_STATE and not self._has_subscribed_state:
            if self.done is not None:
                obj = self.done
            else:
                obj = self.readback
            obj.subscribe(self._run_filt_state, run=False)
            self._has_subscribed_state = True
        return cid

    def _run_filt_state(self, *args, **kwargs):
        kwargs.pop('sub_type')
        kwargs.pop('obj')
        self._run_subs(sub_type=self.SUB_STATE, obj=self, **kwargs)

    def format_status_info(self, status_info):
        """
        Override status info handler to render the att

        Display attenuator status info in the ipython terminal.

        Parameters
        ----------
        status_info: dict
            Nested dictionary. Each level has keys name, kind, and is_device.
            If is_device is True, subdevice dictionaries may follow. Otherwise,
            the only other key in the dictionary will be value.
        Returns
        -------
        status: str
            Formatted string with all relevant status information.

        """
        # Get the attenuator statuses
        blade_states = []
        for i in range(1, MAX_FILTERS + 1):
            try:
                filter_info = status_info[f'filter{i}']
            except KeyError:
                break
            status = get_status_value(filter_info, 'status', 'value')
            blade_states.append(status)

        states = '\n'.join(render_ascii_att(blade_states))

        energy = get_status_float(
            status_info, 'energy', 'value', scale=1e-3, precision=3)
        energy_3rd = get_status_float(
            status_info, 'energy_3rd', 'value', scale=1e-3, precision=3)
        trans = get_status_float(
            status_info, 'position', precision=4, format='E')
        trans_3rd = get_status_float(
            status_info, 'readback_3rd', 'value', precision=4, format='E')

        if energy_3rd != 'N/A':
            status_3rd = (
                f'Transmission for 3rd harmonic (E={energy_3rd} keV): '
                f'{trans_3rd}'
            )
        else:
            status_3rd = ''

        return f"""\
{states}
Transmission for 1st harmonic (E={energy} keV): {trans}
{status_3rd}
"""


class AttBaseWith3rdHarmonic(AttBase):
    """
    Base class for attenuators with 3rd harmonic frequency.

    This base class contains 3rd harmonic frequncy components.
    You should not instantiate this class directly, but instead use the
    :func:`Attenuator` factory function.
    """
    # Positioner Signals
    setpoint_3rd = Cpt(EpicsSignal, ':COM:R3_DES', kind='normal')
    readback_3rd = Cpt(EpicsSignalRO, ':COM:R3_CUR', kind='hinted')

    # Attenuator Signals
    energy_3rd = Cpt(EpicsSignalRO, ':COM:T_CALC.VALH', kind='normal')
    trans_ceil_3rd = Cpt(EpicsSignalRO, ':COM:R3_CEIL', kind='omitted')
    trans_floor_3rd = Cpt(EpicsSignalRO, ':COM:R3_FLOOR', kind='omitted')
    user_energy_3rd = Cpt(EpicsSignal, ':COM:E3DES', kind='omitted')


class AttBaseWith3rdHarmonicLP(AttBaseWith3rdHarmonic, LightpathInOutCptMixin):
    """
    Base class for Lightpath-compatible attenuators with 3rd
    harmonic frequency.
    You should not instantiate this class directly, but instead use the
    :func:`Attenuator` factory function.
    """
    # dummy component list to satisfy Mixin checks
    lightpath_cpts = ['dummy']


class FeeAtt(AttBase, PVPositionerPC, LightpathInOutCptMixin):
    """Old attenuator IOC in the FEE."""
    # Positioner Signals
    setpoint = Cpt(EpicsSignal, ':RDES', kind='normal')
    readback = Cpt(EpicsSignal, ':RACT', kind='hinted')
    actuate = Cpt(EpicsSignal, ':GO', kind='omitted')
    done = None

    # Attenuator Signals
    energy = Cpt(EpicsSignalRO, ':ETOA.E', kind='normal')
    trans_ceil = Cpt(EpicsSignalRO, ':R_CEIL', kind='omitted')
    trans_floor = Cpt(EpicsSignalRO, ':R_FLOOR', kind='omitted')
    user_energy = Cpt(EpicsSignal, ':EDES', kind='omitted')
    eget_cmd = Cpt(EpicsSignal, ':EACT.SCAN', kind='omitted')

    # status = None
    calcpend = Cpt(Signal, value=0)

    # Hardcode filters for FEE, because there is only one.
    filter1 = FCpt(FeeFilter, '{self._filter_prefix}1')
    filter2 = FCpt(FeeFilter, '{self._filter_prefix}2')
    filter3 = FCpt(FeeFilter, '{self._filter_prefix}3')
    filter4 = FCpt(FeeFilter, '{self._filter_prefix}4')
    filter5 = FCpt(FeeFilter, '{self._filter_prefix}5')
    filter6 = FCpt(FeeFilter, '{self._filter_prefix}6')
    filter7 = FCpt(FeeFilter, '{self._filter_prefix}7')
    filter8 = FCpt(FeeFilter, '{self._filter_prefix}8')
    filter9 = FCpt(FeeFilter, '{self._filter_prefix}9')
    num_att = 9

    lightpath_cpts = [f'filter{x}' for x in range(1, 10)]

    def __init__(self, prefix='SATT:FEE1:320', *, name='FeeAtt', **kwargs):
        self._filter_prefix = prefix[:-1]
        super().__init__(prefix, name=name, **kwargs)


def _make_att_classes(max_filters, base_with_3rd_harmonic, name):
    """Generate all possible subclasses."""
    att_classes = {}
    for i in range(1, max_filters + 1):
        att_ns = {}
        for n in range(1, i + 1):
            comp = Cpt(Filter, f':{n:02}')
            att_ns[f'filter{n}'] = comp

        if issubclass(base_with_3rd_harmonic, LightpathInOutCptMixin):
            att_ns['lightpath_cpts'] = [
                f'filter{i}' for i in range(1, i + 1)
            ]
        cls_name = f'{name}{i}'
        cls = type(cls_name, (base_with_3rd_harmonic,), att_ns)
        cls.num_att = i
        att_classes[i] = cls
    return att_classes


_att_classes = _make_att_classes(
    MAX_FILTERS, AttBaseWith3rdHarmonic, 'Attenuator')
_lightpath_att_classes = _make_att_classes(
    MAX_FILTERS, AttBaseWith3rdHarmonicLP, 'Attenuator')


def Attenuator(prefix, n_filters, *, name, **kwargs):
    """
    A series of filters that attenuates the beam.

    This is a factory function for instantiating a subclass of :class:`AttBase`
    with the correct number of :class:`Filter` components.

    The :class:`Filter` components will be named 'filter1', 'filter2', ...
    'filter10', ...

    Parameters
    ----------
    prefix : str
        The EPICS prefix that identifies the attenuator, e.g. 'XPP:ATT'

    n_filters : int
        The number of filters in the attenuator.

    name : str
        An identifying name for the attenuator.
    """
    if 'input_branches' in kwargs:
        cls = _lightpath_att_classes[n_filters]
    else:
        cls = _att_classes[n_filters]
    return cls(prefix, name=name, **kwargs)


'''
# WIP
def set_combined_attenuation(attenuation, *attenuators):
    for i in range(len(attenuators)):
        if i < len(attenuators)-1:
            attenuators[i].actuate_value(force_ceil=True)
        else:
            attenuators[i].actuate_value()
'''


class FEESolidAttenuatorStates(TwinCATInOutPositioner):
    """
    The states class for a standard in/out attenuator blade.

    Defines the state count as 2 (OUT and IN) to limit the number of
    config PVs we connect to.
    """
    config = UpCpt(state_count=2)


class SXRLadderAttenuatorStates(TwinCATInOutPositioner):
    """
    The states class for the SXR Ladder-style attenuators.

    Defines the state count as 9 (OUT and 8 targets) to limit the
    number of config PVs we connect to.
    """
    config = UpCpt(state_count=9)


class FEESolidAttenuatorBlade(BaseInterface, Device):
    """
    Represents one basic solid attenuator blade.

    This includes the binary in/out state and a raw motor.
    """
    state = Cpt(FEESolidAttenuatorStates, ':STATE')
    motor = Cpt(BeckhoffAxisNoOffset, '')


class SXRLadderAttenuatorBlade(FEESolidAttenuatorBlade):
    """
    Represents one ladder solid attenuator blade.

    This includes the out/8 targets state and a raw motor.
    """
    state = Cpt(SXRLadderAttenuatorStates, ':STATE')


class GasAttenuator(BaseInterface, Device):
    """
    AT*:GAS, Base class for an LCLS-II XTES gas attenuator.

    Parameters
    ----------
    prefix : str
        Full Gas Attenuator base PV.

    name : str
        Alias for the Gas Attenuator.

    Notes
    -----
    The HXR gas attenuator was not recommissioned so this class alone
    represents the gas attenuators present at this time.
    """

    not_implemented = Cpt(SignalRO, name="Not Implemented",
                          value="Not Implemented", kind='normal')


class AttenuatorCalculatorFilter(BaseInterface, Device):
    material = Cpt(
        EpicsSignal, 'Material', kind='hinted', string=True,
        doc='The material formula (e.g., Si, C)'
    )
    thickness = Cpt(
        EpicsSignal, 'Thickness', kind='hinted',
        doc='Thickness in micron',
    )
    active = Cpt(
        EpicsSignal, 'Active', kind='normal',
        doc='Should the filter be used in calculations?',
    )
    is_stuck = Cpt(
        EpicsSignal, 'IsStuck', kind='hinted',
        doc='Is the filter stuck / unusable?',
    )
    closest_energy = Cpt(
        EpicsSignalRO, 'ClosestEnergy_RBV', kind='config',
        doc='Closest tabulated energy available to the requested one',
    )
    transmission = Cpt(EpicsSignalRO, 'Transmission_RBV', kind='normal',
                       doc='Normalized transmission at the reported energy',
                       )
    set_metadata(transmission, dict(variety='scalar',
                                    display_format='exponential'))

    transmission_3omega = Cpt(
        EpicsSignalRO, 'Transmission3Omega_RBV', kind='normal',
        doc='Normalized transmission at 3 * the reported energy',
    )
    set_metadata(transmission_3omega, dict(variety='scalar',
                                           display_format='exponential'))

    def __init__(self, *args, index, **kwargs):
        super().__init__(*args, **kwargs)
        self.index = index


class AttenuatorCalculatorBase(BaseInterface, Device):
    """Base class for new-style caproto IOC attenuator calculator devices."""

    # QIcon for UX
    _icon = 'fa.barcode'

    calc_mode = Cpt(
        EpicsSignal, ':SYS:CalcMode', kind='config', string=True,
        doc='Floor or Ceiling calculation',
    )

    energy_source = Cpt(
        EpicsSignal, ':SYS:EnergySource', kind='config', string=True,
        doc='Use beamline photon energy or custom energy?',
    )

    energy_custom = Cpt(
        EpicsSignal, ':SYS:CustomPhotonEnergy', kind='config',
        doc='Custom energy to use for calculations [eV]',
    )

    energy_actual = Cpt(
        EpicsSignalRO, ':SYS:ActualPhotonEnergy_RBV', kind='normal',
        doc='The reported beamline photon energy [eV]',
    )

    actual_transmission = Cpt(
        EpicsSignalRO, ':SYS:ActualTransmission_RBV', kind='normal',
        doc='Actual normalized transmission value',
    )
    set_metadata(actual_transmission,
                 dict(variety='scalar', display_format='exponential'))

    actual_transmission_3omega = Cpt(
        EpicsSignalRO, ':SYS:Actual3OmegaTransmission_RBV', kind='normal',
        doc='Actual 3 omega normalized transmission value',
    )
    set_metadata(actual_transmission_3omega,
                 dict(variety='scalar', display_format='exponential'))

    desired_transmission = Cpt(
        EpicsSignal, ':SYS:DesiredTransmission', kind='normal',
        doc='Desired normalized transmission value',
    )
    set_metadata(desired_transmission, dict(variety='scalar',
                                            display_format='exponential'))

    last_energy = Cpt(
        EpicsSignalRO, ':SYS:LastPhotonEnergy_RBV', kind='config',
        doc=('The photon energy used for the previous calculation; i.e., '
             'the one that goes along with `best_config`.'),
    )

    # NOTE: this variant exists as well but duplicates the bitmask information:
    best_config = Cpt(
        EpicsSignalRO, ':SYS:BestConfiguration_RBV', kind='normal',
        doc='The best configuration of filters for the desired transmission',
    )
    # set_metadata(best_config, dict(variety='array-nd'))
    # # TODO: array-tabular would be nice, but does not work in typhos yet

    best_config_bitmask = Cpt(
        EpicsSignalRO, ':SYS:BestConfigurationBitmask_RBV', kind='normal',
        doc='The best configuration of filters for the desired transmission.',
    )
    set_metadata(best_config_bitmask, dict(variety='bitmask', bits=18))
    # TODO: array-tabular would be nice, but does not work in typhos yet

    best_config_error = Cpt(
        EpicsSignalRO, ':SYS:BestConfigError_RBV', kind='normal',
        doc='Desired to calculated transmission error',
    )

    # NOTE: this variant exists as well but duplicates the bitmask information:
    active_config = Cpt(
        EpicsSignalRO, ':SYS:ActiveConfiguration_RBV', kind='omitted',
        doc='Where the filters are now',
    )
    # set_metadata(active_config, dict(variety='array-nd'))
    # TODO: array-tabular would be nice, but does not work in typhos yet

    active_config_bitmask = Cpt(
        EpicsSignalRO, ':SYS:ActiveConfigurationBitmask_RBV', kind='normal',
        doc='Where the filters are now (as an integer)',
    )
    set_metadata(active_config_bitmask, dict(variety='bitmask', bits=18))

    # NOTE: this variant exists as well but duplicates the bitmask information:
    filters_moving = Cpt(
        EpicsSignalRO, ':SYS:FiltersMoving_RBV', kind='normal',
        doc='Filter-by-filter motion status (1 if moving)',
    )
    # set_metadata(filters_moving, dict(variety='array-nd'))

    filters_moving_bitmask = Cpt(
        EpicsSignalRO, ':SYS:FiltersMovingBitmask_RBV', kind='normal',
        doc='Filter-by-filter motion status as a bitmask',
    )
    set_metadata(filters_moving_bitmask, dict(variety='bitmask', bits=18))

    run_calculation = Cpt(
        EpicsSignal, ':SYS:Run', kind='config',
        doc='Start the calculation',
    )
    set_metadata(run_calculation, dict(variety='command-proc', value=1))

    apply_config = Cpt(
        EpicsSignal, ':SYS:ApplyConfiguration', kind='config',
        doc='Apply the best configuration (i.e., move the filters)',
    )
    set_metadata(apply_config, dict(variety='command-proc', value=1))

    moving = Cpt(
        EpicsSignalRO, ':SYS:Moving_RBV', kind='config',
        doc='Are filters being moved in/out?',
    )
    set_metadata(moving, dict(variety='bitmask', bits=1))

    def __init__(self, prefix, *, name, **kwargs):
        super().__init__(prefix, name=name, **kwargs)
        if self._filter_parent is not None:
            filter_parent = getattr(self, self._filter_parent)
        else:
            filter_parent = self

        self.filters_by_index = {
            index: getattr(filter_parent, attr)
            for index, attr in self._filter_index_to_attr.items()
        }

    def get_active_config(self, **kwargs):
        """Get the active filter configuration."""
        return list(self.active_config.get(**kwargs))

    def get_best_config(self, **kwargs):
        """Get the calculated (best) filter configuration."""
        return list(self.best_config.get(**kwargs))

    def get_moving_status(self, **kwargs):
        """Get the filter motion status."""
        return list(self.filters_moving.get(**kwargs))

    def calculate(self, transmission, *, energy=None, use_floor=True):
        """
        Calculate a blade configuration given a desired transmission value.

        If ``energy`` is not specified, this method defaults to using the
        current L-line photon energy, as reported by the Photon Machine
        Protection System: ``PMPS:LFE:PE:UND:CurrentPhotonEnergy_RBV``.

        Parameters
        ----------
        transmission : float
            The desired transmission, in the range [0, 1].

        energy : float, optional
            The photon energy to use for the calculation.

        use_floor : bool, optional
            Select floor or ceiling transmission estimation.  Defaults to
            floor.
        """

        if energy is not None:
            self.energy_source.put('Custom')
            self.energy_custom.put(float(energy))
        else:
            self.energy_source.put('Actual')

        self.calc_mode.put('Floor' if use_floor else 'Ceiling')
        self.desired_transmission.put(transmission)
        self.run_calculation.put(1, wait=True)
        return self.get_best_config(use_monitor=False)


class AttenuatorCalculator_AT2L0(AttenuatorCalculatorBase):
    """
    Solid attenuator variant from the LCLS-II XTES project.

    Parameters
    ----------
    prefix : str
        Full Solid Attenuator base PV.

    name : str
        Alias for the Solid Attenuator.
    """

    tab_component_names = True
    first_filter = 2
    num_filters = 18
    # "filters" DDC holds all the individual components:
    _filter_parent = 'filters'
    _filter_index_to_attr = {
        idx: f'filter_{idx:02d}' for idx in range(first_filter,
                                                  num_filters + first_filter)
    }

    # Creates filters from 2 to num_filters, with attributes filter_02 and so
    # on.
    filters = DDC(
        {attr: (AttenuatorCalculatorFilter,
                f':FILTER:{idx:02d}:',
                {'index': idx})
         for idx, attr in _filter_index_to_attr.items()
         }
    )

    def format_status_info(self, status_info):
        """
        Override status info handler to render the attenuator.
        """
        table = utils.format_status_table(
            status_info.get('filters', {}),
            row_to_key=self._filter_index_to_attr,
            column_to_key={
                'Active': 'active',
                'Material': 'material',
                'Thickness [um]': 'thickness',
                'Stuck': 'is_stuck',
                'Transmission': 'transmission',
                'Transmission 3 Omega': 'transmission_3omega',
            },
            row_identifier='Filter',
        )

        return str(table)


class AttenuatorCalculatorSXR_Blade(AttenuatorCalculatorFilter):
    # TODO FltMvInterface?
    """
    A single blade, holding up to 8 filters.
    """
    tab_component_names = True
    filter_01 = Cpt(AttenuatorCalculatorFilter, 'FILTER:01:', index=1)
    filter_02 = Cpt(AttenuatorCalculatorFilter, 'FILTER:02:', index=2)
    filter_03 = Cpt(AttenuatorCalculatorFilter, 'FILTER:03:', index=3)
    filter_04 = Cpt(AttenuatorCalculatorFilter, 'FILTER:04:', index=4)
    filter_05 = Cpt(AttenuatorCalculatorFilter, 'FILTER:05:', index=5)
    filter_06 = Cpt(AttenuatorCalculatorFilter, 'FILTER:06:', index=6)
    filter_07 = Cpt(AttenuatorCalculatorFilter, 'FILTER:07:', index=7)
    filter_08 = Cpt(AttenuatorCalculatorFilter, 'FILTER:08:', index=8)
    inserted_filter_index = Cpt(EpicsSignalRO, 'InsertedFilter_RBV',
                                kind='normal')

    _filter_index_to_attr = {
        1: 'filter_01',
        2: 'filter_02',
        3: 'filter_03',
        4: 'filter_04',
        5: 'filter_05',
        6: 'filter_06',
        7: 'filter_07',
        8: 'filter_08',
    }

    def format_status_info(self, status_info):
        """
        Override status info handler to render the attenuator blade.
        """
        inserted_filter = get_status_value(
            status_info, 'inserted_filter_index', 'value')
        material = get_status_value(status_info, 'material', 'value')
        thickness = get_status_value(status_info, 'thickness', 'value')
        transmission = get_status_value(
            status_info, 'transmission', 'value', default_value=0.0)
        transmission3 = get_status_value(
            status_info, 'transmission_3omega', 'value', default_value=0.0)
        table = utils.format_status_table(
            status_info,
            row_to_key=self._filter_index_to_attr,
            column_to_key={
                'Active': 'active',
                'Material': 'material',
                'Thickness [um]': 'thickness',
                'Stuck': 'is_stuck',
                'Transmission': 'transmission',
                'Transmission 3 Omega': 'transmission_3omega',
            },
            row_identifier='Filter',
        )

        if inserted_filter is not None and inserted_filter > 1:
            # Subtract 1 from the filter to match state -> filter index
            inserted_info = (
                f'Inserted filter: #{inserted_filter - 1} ('
                f'{material} {thickness} um T={transmission} '
                f'T3={transmission3})'
            )
        else:
            inserted_info = 'Inserted filter: None'

        return f'''\
{inserted_info}

{table}
'''


class AttenuatorCalculatorSXR_TwoBlade(AttenuatorCalculatorBase):
    """
    2 blade x 8 filter solid attenuator variant from the L2SI project.

    Parameters
    ----------
    prefix : str
        Full Solid Attenuator base PV.

    name : str
        Alias for the Solid Attenuator.
    """

    tab_component_names = True
    first_filter = 1
    num_filters = 2
    # Not using "DDC" here, so the parent is `self`:
    _filter_parent = None
    _filter_index_to_attr = {
        1: 'blade_01',
        2: 'blade_02',
    }

    blade_01 = Cpt(AttenuatorCalculatorSXR_Blade, ':AXIS:01:', index=1)
    blade_02 = Cpt(AttenuatorCalculatorSXR_Blade, ':AXIS:02:', index=2)

    def format_status_info(self, status_info):
        """
        Override status info handler to render the attenuator.
        """
        return utils.combine_status_info(
            self, status_info, self._filter_index_to_attr.values(),
        )


class AttenuatorCalculatorSXR_FourBlade(AttenuatorCalculatorBase):
    """
    4 blade x 8 filter solid attenuator variant from the L2SI project.

    Parameters
    ----------
    prefix : str
        Full Solid Attenuator base PV.

    name : str
        Alias for the Solid Attenuator.
    """

    tab_component_names = True
    first_filter = 1
    num_filters = 4
    # Not using "DDC" here, so the parent is `self`:
    _filter_parent = None
    _filter_index_to_attr = {
        1: 'blade_01',
        2: 'blade_02',
        3: 'blade_03',
        4: 'blade_04',
    }

    blade_01 = Cpt(AttenuatorCalculatorSXR_Blade, ':AXIS:01:', index=1)
    blade_02 = Cpt(AttenuatorCalculatorSXR_Blade, ':AXIS:02:', index=2)
    blade_03 = Cpt(AttenuatorCalculatorSXR_Blade, ':AXIS:03:', index=3)
    blade_04 = Cpt(AttenuatorCalculatorSXR_Blade, ':AXIS:04:', index=4)

    def format_status_info(self, status_info):
        """
        Override status info handler to render the attenuator.
        """
        return utils.combine_status_info(
            self, status_info, self._filter_index_to_attr.values(),
        )


class AttenuatorSXR_Ladder(FltMvInterface, PVPositionerPC,
                           LightpathMixin):
    """
    Ladder-style solid attenuator variant from the LCLS-II L2SI project.

    This has 4 blades, each with up to 8 filters each.
    This class includes a calculator to aid in determining which filters to
    insert for a given attenuation at a specific energy.

    Parameters
    ----------
    prefix : str
        Solid Attenuator base PV.

    name : str
        Alias for the Solid Attenuator.

    calculator_prefix : str
        The prefix for the calculator PVs.
    """

    # QIcon for UX
    _icon = 'fa.barcode'
    tab_component_names = True

    # Register that all blades are needed for lightpath calc
    lightpath_cpts = [f'blade_{idx:02}.state.state' for idx in range(1, 5)]

    # Summary for lightpath view
    num_in = Cpt(InternalSignal, kind='hinted')
    num_out = Cpt(InternalSignal, kind='hinted')

    calculator = UCpt(AttenuatorCalculatorSXR_FourBlade)
    blade_01 = Cpt(SXRLadderAttenuatorBlade, ':MMS:01')
    blade_02 = Cpt(SXRLadderAttenuatorBlade, ':MMS:02')
    blade_03 = Cpt(SXRLadderAttenuatorBlade, ':MMS:03')
    blade_04 = Cpt(SXRLadderAttenuatorBlade, ':MMS:04')

    flow_meter = Cpt(FDQ, '', kind='normal',
                     doc='Device that measures PCW Flow Rate.')

    def __init__(self, *args, limits=None, **kwargs):
        UCpt.collect_prefixes(self, kwargs)
        limits = limits or (0.0, 1.0)
        super().__init__(*args, limits=limits, **kwargs)

    @property
    def setpoint(self):
        """(PVPositioner compat) - use desired transmission as setpoint."""
        return self.calculator.desired_transmission

    @property
    def readback(self):
        """(PVPositioner compat) - use actual transmission as readback."""
        return self.calculator.actual_transmission

    @property
    def actuate(self):
        """(PVPositioner compat) - use apply_config as an actuation signal."""
        return self.calculator.apply_config

    def _setup_move(self, position):
        """(PVPositioner compat) - calculate, then move."""
        # Do not call `calculator.calculate()` here to respect the current
        # calculator settings:
        self.calculator.desired_transmission.put(position)
        self.calculator.run_calculation.put(1, wait=True)
        return super()._setup_move(position)

    def get_lightpath_state(self, use_cache=True) -> LightpathState:
        """
        Grab slightly different PV values for use in same inout calc fn
        The state is nested one device deeper than LightpathInOutCptMixin
        expects.
        """
        if (not use_cache) or (self._cached_state is None):
            lightpath_kwargs = {}
            lp_sigs = self.lightpath_summary._signals.keys()
            for sig in lp_sigs:
                # want to get name of blade_0x from dev_blade_0x_state_state
                cpt_name = sig.name.removeprefix(self.name + '_')
                cpt_name = cpt_name.removesuffix('_state_state')
                lightpath_kwargs[cpt_name] = sig.get()

            self._cached_state = self.calc_lightpath_state(**lightpath_kwargs)

        return self._cached_state

    def calc_lightpath_state(self, **lightpath_kwargs) -> LightpathState:
        # Repeat lightpath logic to extract num_in, num_out
        in_check = []
        out_check = []
        trans_check = []
        for sig_name, sig_value in lightpath_kwargs.items():
            # InOut positioner is not the parent component, but the .state
            obj = getattr(self, sig_name).state
            if not obj._state_initialized:
                # This would prevent make check_inserted, etc. fail
                if self._retry_lightpath:
                    self._retry_lightpath = False
                    utils.schedule_task(self._calc_cache_lightpath_state,
                                        delay=2.0)

                return LightpathState(
                    inserted=True,
                    removed=True,
                    output={self.output_branches[0]: 1}
                )

            self._retry_lightpath = True
            # get state of the InOutPositioner and check status
            in_check.append(obj.check_inserted(sig_value))
            out_check.append(obj.check_removed(sig_value))
            trans_check.append(obj.check_transmission(sig_value))
        self._inserted = any(in_check)
        self._removed = all(out_check)
        self._transmission = functools.reduce(lambda a, b: a*b, trans_check)

        self.num_in.put(in_check.count(True), force=True)
        self.num_out.put(out_check.count(True), force=True)
        return LightpathState(
            inserted=self._inserted,
            removed=self._removed,
            output={self.output_branches[0]: self._transmission}
        )

    def format_status_info(self, status_info):
        """
        Override status info handler to render the attenuator.
        """
        calc_status = status_info.get('calculator', {})
        transmission = get_status_float(
            calc_status, 'actual_transmission', 'value',
            format='E', precision=3,
        )
        transmission_3 = get_status_float(
            calc_status, 'actual_transmission_3omega', 'value',
            format='E', precision=3,
        )
        energy = get_status_float(
            calc_status, 'energy_actual', 'value',
            scale=1e-3,
        )
        energy_3 = get_status_float(
            calc_status, 'energy_actual', 'value',
            scale=3 * 1e-3,
        )
        blade_names = [cpt.split('.', 1)[0] for cpt in self.lightpath_cpts]
        cpt_states = [
            get_status_value(
                status_info, cpt, 'state', 'state', 'value',
                default_value=0
            )
            for cpt in blade_names
        ]

        table = prettytable.PrettyTable()
        table.field_names = ['State'] + list(blade_names)
        for state in LadderBladeState:
            row = [state.name] + ['X' if cpt_state == state.value else ''
                                  for cpt_state in cpt_states]
            table.add_row(row)

        return f"""
{table}
Transmission (E={energy} keV): {transmission}
Transmission for 3rd harmonic (E={energy_3} keV): {transmission_3}
"""


class AttenuatorSXR_LadderTwoBladeLBD(FltMvInterface, PVPositionerPC,
                                      LightpathMixin):
    """
    Ladder-style solid attenuator variant from the LCLS-II L2SI project.

    This has 2 blades, each with up to 8 filters each.
    This class includes a calculator to aid in determining which filters to
    insert for a given attenuation at a specific energy.
    This class also includes control for the Kurt J. Lesker LBD stage.

    Parameters
    ----------
    prefix : str
        Solid Attenuator base PV.

    name : str
        Alias for the Solid Attenuator.

    calculator_prefix : str
        The prefix for the calculator PVs.
    """

    # QIcon for UX
    _icon = 'fa.barcode'
    tab_component_names = True

    # Register that all blades are needed for lightpath calc
    lightpath_cpts = [f'blade_{idx:02}.state.state' for idx in range(1, 4)]

    # Summary for lightpath view
    num_in = Cpt(InternalSignal, kind='hinted')
    num_out = Cpt(InternalSignal, kind='hinted')

    calculator = UCpt(AttenuatorCalculatorSXR_TwoBlade)
    blade_01 = Cpt(SXRLadderAttenuatorBlade, ':MMS:01')
    blade_02 = Cpt(SXRLadderAttenuatorBlade, ':MMS:02')
    # LBD Stage
    blade_03 = Cpt(FEESolidAttenuatorBlade, ':MMS:03')

    flow_meter = Cpt(FDQ, '', kind='normal',
                     doc='Device that measures PCW Flow Rate.')

    def __init__(self, *args, limits=None, **kwargs):
        UCpt.collect_prefixes(self, kwargs)
        limits = limits or (0.0, 1.0)
        super().__init__(*args, limits=limits, **kwargs)

    @property
    def setpoint(self):
        """(PVPositioner compat) - use desired transmission as setpoint."""
        return self.calculator.desired_transmission

    @property
    def readback(self):
        """(PVPositioner compat) - use actual transmission as readback."""
        return self.calculator.actual_transmission

    @property
    def actuate(self):
        """(PVPositioner compat) - use apply_config as an actuation signal."""
        return self.calculator.apply_config

    def _setup_move(self, position):
        """(PVPositioner compat) - calculate, then move."""
        # Do not call `calculator.calculate()` here to respect the current
        # calculator settings:
        self.calculator.desired_transmission.put(position)
        self.calculator.run_calculation.put(1, wait=True)
        return super()._setup_move(position)

    def get_lightpath_state(self, use_cache=True) -> LightpathState:
        """
        Grab slightly different PV values for use in same inout calc fn
        The state is nested one device deeper than LightpathInOutCptMixin
        expects.
        """
        if (not use_cache) or (self._cached_state is None):
            lightpath_kwargs = {}
            lp_sigs = self.lightpath_summary._signals.keys()
            for sig in lp_sigs:
                # want to get name of blade_0x from dev_blade_0x_state_state
                cpt_name = sig.name.removeprefix(self.name + '_')
                cpt_name = cpt_name.removesuffix('_state_state')
                lightpath_kwargs[cpt_name] = sig.get()

            self._cached_state = self.calc_lightpath_state(**lightpath_kwargs)

        return self._cached_state

    def calc_lightpath_state(self, **lightpath_kwargs) -> LightpathState:
        # Repeat lightpath logic to extract num_in, num_out
        in_check = []
        out_check = []
        trans_check = []
        for sig_name, sig_value in lightpath_kwargs.items():
            # InOut positioner is not the parent component, but the .state
            obj = getattr(self, sig_name).state
            if not obj._state_initialized:
                # This would prevent make check_inserted, etc. fail
                if self._retry_lightpath:
                    self._retry_lightpath = False
                    utils.schedule_task(self._calc_cache_lightpath_state,
                                        delay=2.0)

                return LightpathState(
                    inserted=True,
                    removed=True,
                    output={self.output_branches[0]: 1}
                )

            self._retry_lightpath = True
            # get state of the InOutPositioner and check status
            in_check.append(obj.check_inserted(sig_value))
            out_check.append(obj.check_removed(sig_value))
            trans_check.append(obj.check_transmission(sig_value))
        self._inserted = any(in_check)
        self._removed = all(out_check)
        self._transmission = functools.reduce(lambda a, b: a*b, trans_check)

        self.num_in.put(in_check.count(True), force=True)
        self.num_out.put(out_check.count(True), force=True)
        return LightpathState(
            inserted=self._inserted,
            removed=self._removed,
            output={self.output_branches[0]: self._transmission}
        )

    def format_status_info(self, status_info):
        """
        Override status info handler to render the attenuator.
        """
        calc_status = status_info.get('calculator', {})
        transmission = get_status_float(
            calc_status, 'actual_transmission', 'value',
            format='E', precision=3,
        )
        transmission_3 = get_status_float(
            calc_status, 'actual_transmission_3omega', 'value',
            format='E', precision=3,
        )
        energy = get_status_float(
            calc_status, 'energy_actual', 'value',
            scale=1e-3,
        )
        energy_3 = get_status_float(
            calc_status, 'energy_actual', 'value',
            scale=3 * 1e-3,
        )
        blade_names = [cpt.split('.', 1)[0] for cpt in self.lightpath_cpts]
        cpt_states = [
            get_status_value(
                status_info, cpt, 'state', 'state', 'value',
                default_value=0
            )
            for cpt in blade_names
        ]

        table = prettytable.PrettyTable()
        table.field_names = ['State'] + list(blade_names)
        for state in LadderBladeState:
            row = [state.name] + ['X' if cpt_state == state.value else ''
                                  for cpt_state in cpt_states]
            table.add_row(row)

        return f"""
{table}
Transmission (E={energy} keV): {transmission}
Transmission for 3rd harmonic (E={energy_3} keV): {transmission_3}
"""


class AT1K4(AttenuatorSXR_Ladder):
    """
    AT1K4 solid attenuator variant from the LCLS-II L2SI project.

    This has 4 blades, each with up to 8 filters each.
    This class includes a calculator to aid in determining which filters to
    insert for a given attenuation at a specific energy.

    Parameters
    ----------
    prefix : str
        Solid Attenuator base PV.

    name : str
        Alias for the Solid Attenuator.

    calculator_prefix : str
        The prefix for the calculator PVs.
    """
    flow_meter = None


class AT1K2(AttenuatorSXR_LadderTwoBladeLBD):
    """
    AT1K2 solid attenuator variant from the LCLS-II L2SI project.

    This has 2 blades, each with up to 8 filters each.
    This class includes a calculator to aid in determining which filters to
    insert for a given attenuation at a specific energy.
    This class also includes control for the Kurt J. Lesker LBD system,
    allowing IN/OUT state for mirror mount.

    Parameters
    ----------
    prefix : str
        Solid Attenuator base PV.

    name : str
        Alias for the Solid Attenuator.

    calculator_prefix : str
        The prefix for the calculator PVs.
    """


class AT2K2(AttenuatorSXR_Ladder):
    """
    AT2K2 solid attenuator variant from the LCLS-II L2SI project.

    This has 4 blades, each with up to 8 filters each.
    This class includes a calculator to aid in determining which filters to
    insert for a given attenuation at a specific energy.

    Parameters
    ----------
    prefix : str
        Solid Attenuator base PV.

    name : str
        Alias for the Solid Attenuator.

    calculator_prefix : str
        The prefix for the calculator PVs.
    """


class AT2L0(FltMvInterface, PVPositionerPC, LightpathMixin):
    """
    AT2L0 solid attenuator variant from the LCLS-II XTES project.

    Motorized, 18 filters + 1 inspection mirror.
    This class includes a calculator to aid in determining which filters to
    insert for a given attenuation at a specific energy.

    Parameters
    ----------
    prefix : str
        Solid Attenuator base PV.

    name : str
        Alias for the Solid Attenuator.
    """

    # QIcon for UX
    _icon = 'fa.barcode'
    tab_component_names = True
    tab_whitelist = ['clear_errors', 'reset_errors']

    # Register that all blades are needed for lightpath calc
    lightpath_cpts = [f'blade_{idx:02}.state.state' for idx in range(1, 20)]

    # Summary for lightpath view
    num_in = Cpt(InternalSignal, kind='hinted')
    num_out = Cpt(InternalSignal, kind='hinted')

    def _get_blade_error_attrs() -> Generator[str, None, None]:
        """Get the blade attribute names used for checking errors."""
        for index in range(1, 20):
            yield f"blade_{index:02d}.state.error"
            yield f"blade_{index:02d}.state.error_id"
            yield f"blade_{index:02d}.state.error_message"
            yield f"blade_{index:02d}.motor.plc.err_code"
            yield f"blade_{index:02d}.motor.user_readback"

    def _check_errors(
        self, mds: MultiDerivedSignal, items: SignalToValue
    ) -> str:
        """check for errors, return a string indicating any errors verbally"""
        errors = []
        # sort out .motor from .motor.plc signals
        for sig, value in items.items():
            if ("motor" in sig.name) ^ ("plc" in sig.name):
                value = int(sig.metadata["severity"])

            if value not in (0, ""):
                errors.append(f"{sig.name}: {value}")

        if errors:
            return "\n".join(["Error summary:"] + errors)

        return "No Errors"

    error_summary = Cpt(
        MultiDerivedSignalRO,
        calculate_on_get=_check_errors,
        attrs=list(_get_blade_error_attrs()),
        doc='summarize the errors at any time on any blade via a string',
    )
    set_metadata(error_summary, dict(variety='text-multiline'))

    def _check_errors_bitmask(
        self, mds: MultiDerivedSignal, items: SignalToValue
    ) -> int:
        """check for errors, return an array of binaries 1=error, 0=no error"""
        errors = []
        blade_errors = []
        for sig, value in items.items():
            if ("motor" in sig.name) ^ ("plc" in sig.name):
                value = int(sig.metadata["severity"])

            if value not in (0, ""):
                blade_errors.append(1)
            else:
                blade_errors.append(0)

        step = 5
        # first blade errors not reported in bit array
        start_index = step
        end_index = 2*step
        for _ in range(1, 19):
            error_count = sum(blade_errors[start_index:end_index])
            errors.append(1 if error_count >= 1 else 0)
            start_index += step
            end_index += step

        decimal_value = 0
        for next_bit in errors:
            decimal_value = decimal_value * 2 + next_bit

        return decimal_value

    error_summary_bitmask = Cpt(
        MultiDerivedSignalRO,
        calculate_on_get=_check_errors_bitmask,
        attrs=list(_get_blade_error_attrs()),
        doc='summarize errors at any time on any blade via a bitmask',
    )
    set_metadata(error_summary_bitmask, dict(variety='bitmask', bits=18))

    def clear_errors(self):
        """Reset all attenuator errors, making the device ready to move."""
        self.reset_errors.put(1)

    def _empty_get(self, mds: MultiDerivedSignal, items: SignalToValue) -> int:
        return 0

    def _reset_errors(
        self, mds: MultiDerivedSignal, value: OphydDataType
    ) -> SignalToValue:
        return {sig: 1 for sig in self.reset_errors.signals}

    reset_errors = Cpt(
        MultiDerivedSignal,
        calculate_on_get=_empty_get,
        calculate_on_put=_reset_errors,
        attrs=sum(
            (
                [
                    f"blade_{_blade:02d}.motor.plc.cmd_err_reset",
                    f"blade_{_blade:02d}.state.reset_cmd",
                ]
                for _blade in range(1, 20)
            ),
            [],
        ),
    )
    set_metadata(reset_errors, dict(variety='command-proc', value=1))

    calculator = UCpt(AttenuatorCalculator_AT2L0)
    blade_01 = Cpt(FEESolidAttenuatorBlade, ':MMS:01')
    blade_02 = Cpt(FEESolidAttenuatorBlade, ':MMS:02')
    blade_03 = Cpt(FEESolidAttenuatorBlade, ':MMS:03')
    blade_04 = Cpt(FEESolidAttenuatorBlade, ':MMS:04')
    blade_05 = Cpt(FEESolidAttenuatorBlade, ':MMS:05')
    blade_06 = Cpt(FEESolidAttenuatorBlade, ':MMS:06')
    blade_07 = Cpt(FEESolidAttenuatorBlade, ':MMS:07')
    blade_08 = Cpt(FEESolidAttenuatorBlade, ':MMS:08')
    blade_09 = Cpt(FEESolidAttenuatorBlade, ':MMS:09')
    blade_10 = Cpt(FEESolidAttenuatorBlade, ':MMS:10')
    blade_11 = Cpt(FEESolidAttenuatorBlade, ':MMS:11')
    blade_12 = Cpt(FEESolidAttenuatorBlade, ':MMS:12')
    blade_13 = Cpt(FEESolidAttenuatorBlade, ':MMS:13')
    blade_14 = Cpt(FEESolidAttenuatorBlade, ':MMS:14')
    blade_15 = Cpt(FEESolidAttenuatorBlade, ':MMS:15')
    blade_16 = Cpt(FEESolidAttenuatorBlade, ':MMS:16')
    blade_17 = Cpt(FEESolidAttenuatorBlade, ':MMS:17')
    blade_18 = Cpt(FEESolidAttenuatorBlade, ':MMS:18')
    blade_19 = Cpt(FEESolidAttenuatorBlade, ':MMS:19')

    def print_errors(self):
        """prints the error summary """
        print(self.error_summary.get())

    @property
    def setpoint(self):
        """(PVPositioner compat) - use desired transmission as setpoint."""
        return self.calculator.desired_transmission

    @property
    def readback(self):
        """(PVPositioner compat) - use actual transmission as readback."""
        return self.calculator.actual_transmission

    @property
    def actuate(self):
        """(PVPositioner compat) - use apply_config as an actuation signal."""
        return self.calculator.apply_config

    def _setup_move(self, position):
        """(PVPositioner compat) - calculate, then move."""
        # Do not call `calculator.calculate()` here to respect the current
        # calculator settings:
        self.calculator.desired_transmission.put(position)
        self.calculator.run_calculation.put(1, wait=True)
        return super()._setup_move(position)

    def __init__(self, *args, limits=None, calculator_prefix='AT2L0:CALC',
                 **kwargs):
        UCpt.collect_prefixes(self, dict(calculator_prefix=calculator_prefix))
        limits = limits or (0.0, 1.0)
        super().__init__(*args, limits=limits, **kwargs)

    def get_lightpath_state(self, use_cache: bool = True) -> LightpathState:
        """
        Grab slightly different PV values for use in same inout calc fn
        The state is nested one device deeper than LightpathInOutCptMixin
        expects.
        """
        if (not use_cache) or (self._cached_state is None):
            lightpath_kwargs = {}
            lp_sigs = self.lightpath_summary._signals.keys()
            for sig in lp_sigs:
                # want to get name of blade_0x from dev_blade_0x_state_state
                cpt_name = sig.name.removeprefix(self.name + '_')
                cpt_name = cpt_name.removesuffix('_state_state')
                lightpath_kwargs[cpt_name] = sig.get()

            self._cached_state = self.calc_lightpath_state(**lightpath_kwargs)

        return self._cached_state

    def calc_lightpath_state(self, **lightpath_kwargs) -> LightpathState:
        # Repeat lightpath logic to extract num_in, num_out
        in_check = []
        out_check = []
        trans_check = []
        for sig_name, sig_value in lightpath_kwargs.items():
            obj = getattr(self, sig_name).state
            if not obj._state_initialized:
                # This would prevent make check_inserted, etc. fail
                if self._retry_lightpath:
                    self._retry_lightpath = False
                    utils.schedule_task(self._calc_cache_lightpath_state,
                                        delay=2.0)

                return LightpathState(
                    inserted=True,
                    removed=True,
                    output={self.output_branches[0]: 1}
                )

            self._retry_lightpath = True
            # get state of the InOutPositioner and check status
            in_check.append(obj.check_inserted(sig_value))
            out_check.append(obj.check_removed(sig_value))
            trans_check.append(obj.check_transmission(sig_value))
        self._inserted = any(in_check)
        self._removed = all(out_check)
        self._transmission = functools.reduce(lambda a, b: a*b, trans_check)

        self.num_in.put(in_check.count(True), force=True)
        self.num_out.put(out_check.count(True), force=True)
        return LightpathState(
            inserted=self._inserted,
            removed=self._removed,
            output={self.output_branches[0]: self._transmission}
        )

    def format_status_info(self, status_info):
        """Override status info handler to render the attenuator."""
        calc_status = status_info.get('calculator', {})
        transmission = get_status_float(
            calc_status, 'actual_transmission', 'value',
            format='E', precision=3,
        )
        transmission_3 = get_status_float(
            calc_status, 'actual_transmission_3omega', 'value',
            format='E', precision=3,
        )
        energy = get_status_float(
            calc_status, 'energy_actual', 'value',
            scale=1e-3,
        )
        energy_3 = get_status_float(
            calc_status, 'energy_actual', 'value',
            scale=3 * 1e-3,
        )
        error_sum = get_status_value(
            status_info, 'error_summary', 'value',
            default_value='No Errors',
        )
        cpt_states = [
            get_status_value(
                status_info, cpt.split('.', 1)[0], 'state', 'state', 'value',
                default_value=0
            )
            for cpt in self.lightpath_cpts
        ]

        table = '\n'.join(render_ascii_att(cpt_states, start_index=1))

        return f"""
{table}
Transmission (E={energy} keV): {transmission}
Transmission for 3rd harmonic (E={energy_3} keV): {transmission_3}
Error Summary: {error_sum}
"""


FEESolidAttenuator = AT2L0  # back-compatibility


class BladeStateEnum(enum.IntEnum):
    Unknown = 0
    OUT = 1
    IN = 2
    STUCK_OUT = 3
    STUCK_IN = 4

    @property
    def as_out_row(self) -> str:
        """Returns ASCII information for "out" row representation."""
        return {
            BladeStateEnum.OUT: 'X',
            BladeStateEnum.IN: '',
            BladeStateEnum.STUCK_OUT: 'S',
            BladeStateEnum.STUCK_IN: '',
        }.get(self, '?')

    @property
    def as_in_row(self) -> str:
        """Returns ASCII information for "in" row representation."""
        return {
            BladeStateEnum.OUT: '',
            BladeStateEnum.IN: 'X',
            BladeStateEnum.STUCK_OUT: '',
            BladeStateEnum.STUCK_IN: 'S',
        }.get(self, '?')


class LadderBladeState(enum.IntEnum):
    """
    SXR attenuator ladder motion states.
    """
    # 'Moving' is also: "unknown" or "between states"
    Moving = 0

    # 'Out' is fixed at 1:
    Out = 1

    # And any "in" states follow:
    In_01 = 2
    In_02 = 3
    In_03 = 4
    In_04 = 5
    In_05 = 6
    In_06 = 7
    In_07 = 8
    In_08 = 9

    @property
    def filter_index(self):
        """The one-based filter index, if inserted."""
        if not self.is_inserted:
            return None
        return self.value - 1

    @property
    def is_inserted(self):
        """Is a filter inserted?"""
        return self not in {LadderBladeState.Moving, LadderBladeState.Out}

    @property
    def is_moving(self) -> bool:
        """Is the blade moving?"""
        return self == LadderBladeState.Moving


class SXRGasAtt(BaseInterface, GroupDevice):
    tab_component_names = True
    tab_whitelist = ['setup_mode']

    transmission = Cpt(EpicsSignal, ':TRANS_RBV', write_pv=':TRANS_SP', kind='hinted',
                       doc='Transmission')
    arb_req = Cpt(EpicsSignalRO, ':TRANS_REQ_RBV', kind='hinted',
                  doc='Requested transmission')
    pressure = Cpt(EpicsSignal, ':GCM:82:PRESS_RBV', write_pv=':CNTRL:SP', kind='hinted',
                   doc='Pressure')
    pressure_setpoint_rbv = Cpt(EpicsSignalRO, ':CNTRL:SP_RBV', kind='omitted',
                                doc='Pressure setpoint')
    mode = Cpt(EpicsSignal, ':MODE_RBV', write_pv=':MODE', string=True, kind='hinted',
               doc='PMPS mode')
    control_enable = Cpt(EpicsSignal, ':CNTRL:ON_RBV', write_pv=':CNTRL:ON', kind='hinted',
                         doc='')
    pressure_control_enable = Cpt(EpicsSignal, ':MODE:PressureControl_RBV', write_pv=':MODE:PressureControl', kind='hinted',
                                  doc='Pressure control mode')
    gas_type = Cpt(EpicsSignalRO, ':GAS_TYPE_RBV', string=True, kind='hinted',
                   doc='Selected gas')
    at_target = Cpt(EpicsSignalRO, ':AtTarget_RBV', string=True, kind='hinted',
                    doc='At target')
    moving = Cpt(EpicsSignalRO, ':Moving_RBV', string=True, kind='hinted',
                 doc='Moving')
    gas_att_ok = Cpt(EpicsSignalRO, ':OK_RBV', string=True, kind='hinted',
                     doc='Ok')
    transmission_setpoint_rbv = Cpt(EpicsSignalRO, ':TRANS_SP_RBV', kind='omitted',
                                    doc='Transmission setpoint')
    pressure_control_valve = Cpt(EpicsSignalRO, ':VCN:70:POS_REQ_RBV', kind='omitted',
                                 doc='Requested position')
    valve_n2 = Cpt(VVC, ':VVC:72', kind='hinted', doc='Valve n2')
    valve_ar = Cpt(VVC, ':VVC:71', kind='hinted', doc='Valve ar')
    valve_pressure_control = Cpt(VCN, ':VCN:70', kind='omitted', doc='Pressure control valve')

    def setup_mode(self, mode, control_type='transmission', gas_type=None):
        """
        Setup gas attenuator to work in "PMPS" or "Local" mode, with either "transmission control" or "pressure control"

        Parameters
        ----------
        mode : str, either "PMPS" or "Local"
        Mode for attenuator.
        control_type : str, optional
            Set control type in "Local" mode, either "transmission" or "pressure" control. The default is 'transmission'.
        gas_type : str, optional
            Change gas type to "N2" or "Ar". The default is None.If None is passed the attenuator uses the current gas.

        """
        if mode is not ('PMPS' or 'Local'):
            print('unrecognizied mode, options are "PMPS" or "Local"')
            return
        elif mode == "Local":
            if control_type is not ('transmission' or 'pressure'):
                print('unrecognizied control type, options are "transmission" or "pressure"')
                return
        if gas_type is not ('N2' or 'Ar' or None):
            print('unrecognizied gas type, options are "N2", "Ar", or None')
            return

        if mode == 'PMPS':
            self.mode.put('PMPS')
        elif mode == 'Local':
            self.mode.put('Local')

        if gas_type is not None:
            self.valve_ar.open_command.put(0)
            self.valve_n2.open_command.put(0)
            if gas_type == 'N2':
                self.valve_n2.open_command.put(1)
            elif gas_type == 'Ar':
                self.valve_ar.open_command.put(1)

        elif mode == 'Local':
            if control_type == 'transmission':
                self.transmission.put(1)
                self.control_enable.put(1)
            elif control_type == 'pressure':
                self.pressure_control_enable.put(1)
                self.control_enable.put(1)
                self.pressure.put(0)


def get_blade_enum(value):
    try:
        return BladeStateEnum[value]
    except KeyError:
        return BladeStateEnum(value)


def render_ascii_att(blade_states, *, start_index=0):
    """
    Creates the attenuator ascii art.

    Parameters
    ----------
    blade_states: list of BladeStateEnum
        The elements of this list represent the current blade states.

    start_index : int, optional
        The starting filter index.

    Returns
    -------
    ascii_lines: list of str
        The lines that should be printed to the screen.
    """

    filter_line = ['filter # ']
    out_line = [' OUT     ']
    in_line = [' IN      ']

    for idx, state in enumerate(blade_states, start_index):
        index_str = str(idx)
        filter_line.append(index_str)
        state_enum = get_blade_enum(state)
        out_line.append(state_enum.as_out_row.center(len(index_str)))
        in_line.append(state_enum.as_in_row.center(len(index_str)))

    separator = '|'
    return [separator.join(filter_line + ['']),
            separator.join(out_line + ['']),
            separator.join(in_line + [''])]
