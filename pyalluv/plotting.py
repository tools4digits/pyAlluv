import logging
import itertools
from weakref import WeakValueDictionary
import numpy as np
import matplotlib as mpl
from matplotlib.cbook import index_of
from matplotlib.collections import PatchCollection
# from matplotlib import docstring
from matplotlib import cbook
# from . import (_api, _path, artist, cbook, cm, colors as mcolors, docstring,
from matplotlib.artist import Artist
# from matplotlib import transforms
# from matplotlib import _api
# import matplotlib.dates as mdates
from matplotlib.path import Path
from matplotlib.patches import Rectangle
import matplotlib.patches as patches
from matplotlib.rcsetup import cycler
from matplotlib.legend import Legend
import matplotlib.ticker as mticker
# from datetime import datetime
from bisect import bisect_left

# TODO: unused so far
_log = logging.getLogger(__name__)

__author__ = 'Jonas I. Liechti'


# TODO: check if cbook has something for this
def _to_valid_arrays(data, attribute, dtype=np.float64):
    if data is None:
        return None
    if hasattr(data, 'index') and hasattr(data, 'values'):
        return data.values
    try:
        data = np.asarray(data, dtype=dtype)
    except ValueError:
        try:
            _data = iter(data)
        except TypeError:
            raise TypeError("'{attr}' must be an iterable sequence and"
                            " should be of type list or numpy.ndarray,"
                            " '{ftype}' is not supported."
                            .format(attr=attribute, ftype=type(data)))
        else:
            data = []
            for i, d in enumerate(_data):
                try:
                    data.append(np.asarray(d, dtype=dtype))
                except (TypeError, ValueError):
                    raise ValueError("{attr} can only contain array-like"
                                     " objects which is not the case at index"
                                     "{eindex}:\n"
                                     "{entry}".format(attr=attribute, eindex=i,
                                                      entry=d))
    return data


def _memship_to_column(membership, absentval=None):
    if absentval in (None, np.nan):
        _mask = ~np.isnan(membership)
    else:
        _mask = membership != absentval
    nbr_blocks = int(np.amax(membership, where=_mask, initial=-1) + 1)
    block_ids, counts = np.unique(membership[_mask], return_counts=True)
    col = np.zeros(nbr_blocks)
    for bid, count in zip(block_ids.astype(int), counts):
        col[bid] = count
    # TODO: this is not for production: make sure group ids start from 0
    assert np.all(col != 0)
    return nbr_blocks, col


def _between_memships_flow(flow_dims, membership_last, membership_next,
                           absentval=None):
    ext = np.zeros(flow_dims[0])
    flowmatrix = np.zeros(flow_dims, dtype=int)
    for m1, m2 in zip(membership_last.astype(int),
                      membership_next.astype(int)):
        if m1 == absentval:  # node was absent in last
            if m2 != absentval:  # node is present in next
                ext[m2] += 1
            else:  # node also absent in next:
                pass
        else:
            if m2 != absentval:  # node is present in both
                flowmatrix[m2, m1] += 1
            else:  # node is absent in next
                # TODO: handle outflows?
                pass
    return flowmatrix, ext


def _update_limits(limits, newmin, newmax):
    if limits is None:
        return newmin, newmax
    omin, omax = limits
    return min(omin, newmin), max(omax, newmax)


class _ArtistProxy:
    """
    Proxy class for `Artist` subclasses used to draw the various element in an
    alluvial diagram.

    This class assembles common properties and relations to :class:`Patch`
    of the elements present in an Alluvial diagram.
    """
    _artistcls = Artist  # derived must overwrite

    def __init__(self, label=None, **kwargs):
        """TODO: write docstring."""
        # TODO: not sure if stale is needed for this
        self.stale = True
        self._tags = []  # list of tag identifiers (label of a tag)
        self._artist = None
        self.set_styling(kwargs)

    def set_tags(self, tags: list):
        """Set a tag for the block."""
        self._tags = tags
        # TODO: register to all tags!

    def get_tags(self):
        """Return the list of tags."""
        return self._tags

    def add_tag(self, tag):
        """Adding a new tag to the proxy."""
        if tag not in self._tags:
            tag.register_proxy(self)
            self._tags.append(tag)

    def remove_tag(self, tag):
        """Removing a tag."""
        if tag in self._tags:
            tag.deregister_proxy(self)
            self._tags.remove(tag)

    # TODO: not sure if needed
    tags = property(get_tags, set_tags)

    @property
    def is_tagged(self):
        """Indicate whether a block belongs to a tag or not."""
        return True if len(self._tags) else False

    def set_property(self, prop, value):
        """TODO: write docstring."""
        self._kwargs.update(cbook.normalize_kwargs({prop: value}))

    def set_styling(self, props):
        """Set the styling of the element."""
        self._original_kwargs = dict(props)
        self._kwargs = cbook.normalize_kwargs(props, self._artistcls)

    def get_styling(self, original=False):
        """Return the custom styling properties of this element."""
        if original:
            return self._original_kwargs
        else:
            return self._kwargs

    def get_styling_prop(self, prop: str, altval=None):
        """
        Return the value of a specific styling property attached to this proxy
        or to one of its Tags.
        If the property is not set *altval* is returned instead.

        Parameters
        ----------
        prop : str
            The normalized name of the property to fetch.
        altval : Any (default: None)
            The value to return in case *prop* is not set.

        Note that aliases are not allowed for *prop*. This is because tags are
        included when trying to get the styling property and a single tag might
        be associated to proxies of various artist subclasses that might not
        all have the same set aliases.
        """
        if self.is_tagged:
            tagprops, _ = self._from_tags()
            # prefer values from tags over the alternative value
            altval = tagprops.get(prop, altval)
        return self._kwargs.get(prop, altval)

    @property
    def is_styled(self,):
        """Indicate if this element has custom styling."""
        return True if self.is_tagged or self._kwargs else False

    def _create_artist(self, ax, **kwargs):
        """Initiate the artist."""
        raise NotImplementedError('Derived must override')

    def _pre_creation(self, ax=None, **non_artits_props):
        """Method handling properties foreign to the attached artist class."""
        pass

    def _applicable_props(self, props):
        """TODO: write docstring."""
        applicable = dict()
        nonapp = dict()
        for k, v in props.items():
            func = getattr(self._artistcls, f"set_{k}", None)
            if not callable(func):
                # do a warning?
                _log.warning(f"{self._artistcls.__name__!r} object "
                             f"has no property {k!r}")
                nonapp[k] = v
            else:
                applicable[k] = v
        return applicable, nonapp

    def _from_tags(self):
        """
        Get styling properties from all tags of this proxy.

        Note that styling properties of more recently added tags are
        prioritized, if multiple tags are present.
        """
        tagprops, nonapp_tagprops = dict(), dict()
        for tag in self._tags:
            _tprops, _natporop = self._applicable_props(tag.get_props(id(self)))
            _tprops = cbook.normalize_kwargs(_tprops, self._artistcls)
            tagprops.update(_tprops)
            nonapp_tagprops.update(_natporop)
        return tagprops, nonapp_tagprops

    def create_artist(self, ax, **props):
        """Create the artist of this proxy."""
        _props = cbook.normalize_kwargs(props, self._artistcls)
        nonapp_props = dict()
        # process the tags
        if self.is_tagged:
            # TODO: gather all properties from its tag
            tagprops, nonapp_props = self._from_tags()
            _props.update(tagprops)
        # finally update with block specific styling
        _props.update(self._kwargs)
        _props, _nonapp_props = self._applicable_props(_props)
        # combine the non-applicable properties from tags and proxy
        nonapp_props.update(_nonapp_props)
        self._pre_creation(ax, **nonapp_props)
        self._create_artist(ax, **_props)

    def add_artist(self, ax):
        """Adding the artist to an axes."""
        raise NotImplementedError('Derived must override')

    def get_artist(self,):
        """TODO: write docstring."""
        if self._artist is None:
            raise ValueError("The artist has not been created.")
        return self._artist


@cbook._define_aliases({"verticalalignment": ["va"],
                        "horizontalalignment": ["ha"], "width": ["w"],
                        "height": ["h"]})
class _Block(_ArtistProxy):
    """
    A Block in an Alluvial diagram.

        Blocks in an Alluvial diagram get their vertical position assigned by a
        layout algorithm and thus after creation. This is the rational to why
        *_Block* inherits directly from `matplotlib.patches.Patch`, rather than
        `matplotlib.patches.PathPatch` or `matplotlib.patches.Rectangle`.
    """
    _artistcls = Rectangle

    # TODO uncomment once in mpl
    # @docstring.dedent_interpd
    def __init__(self, height, xa=None, ya=None, label=None,
                 tag=None, horizontalalignment='left',
                 verticalalignment='bottom', label_margin=(0, 0),
                 **kwargs):
        """
        Parameters
        -----------
        height : float
          Height of the block.
        xa: scalar, optional
          The x coordinate of the block's anchor point.
        ya: scalar, optional
          The y coordinate of the block's anchor point.
        width : float,  optional
          Block width.
        label : str, optional
          Block label that can be displayed in the diagram.
        horizontalalignment : {'center', 'left', 'right'}, default: 'center'
          The horizontal location of the anchor point of the block.
        verticalalignment: {'center', 'top', 'bottom'}, default: 'center'
          The vertical location of the anchor point of the block.
        label_margin: (float, float), default: (0., 0.)
            x and y margin in target coordinates of ``self.get_transform()``
            and added to the *anchor* point to set the point to draw the label.

        Other Parameters
        ----------------
        **kwargs : Allowed are all `.Rectangle` properties:

          TODO: set to Rectangle (if it is registered)
          %(Patch_kwdoc)s

        """
        super().__init__(label=label, **kwargs)

        self._height = height
        self._width = None
        self._xa = xa
        self._ya = ya
        self._set_horizontalalignment(horizontalalignment)
        self._set_verticalalignment(verticalalignment)
        # init the in and out flows:
        self._outflows = []
        self._inflows = []
        self._label = label
        self.label_margin = label_margin
        # TODO: replace dict with list
        self.in_margin = {'bottom': 0, 'top': 0}
        self.out_margin = {'bottom': 0, 'top': 0}

    def get_xa(self):
        """Return the x coordinate of the anchor point."""
        return self._xa

    def get_ya(self):
        """Return the y coordinate of the anchor point."""
        return self._ya

    def get_xlim(self,):
        """Returns the horizontal data limits as a tuple (x0, width)"""
        return self.get_x(), self.get_width()

    def get_ylim(self,):
        """Returns the vertical data limits as a tuple (y0, height)"""
        return self.get_y(), self.get_height()

    def get_datalim(self,):
        """Return the bounds (x0, y0, width, height) in data coordinates."""
        x0, width = self.get_xlim()
        y0, height = self.get_ylim()
        return x0, y0, width, height

    def get_bounds(self,):
        """Return the bounds (x0, y0, width, height) of `.Bbox`."""
        return self._artist.get_bbox().bounds

    def get_height(self):
        """Return the height of the block."""
        if self._artist is not None:
            return self._artist.get_height()
        else:
            return self._height

    def get_width(self):
        """Return the width of the block."""
        # TODO: this should be functionalized for other attributes
        try:
            return self._artist.get_width()
        except AttributeError:
            return self._width

    def get_anchor(self,):
        """Return the anchor point of the block."""
        return self._anchor

    def get_x(self):
        """Return the left coordinate of the block."""
        x0 = self._xa
        if self._horizontalalignment == 'center':
            x0 -= 0.5 * self.get_width()
        elif self._horizontalalignment == 'right':
            x0 -= self.get_width()
        return x0

    def get_y(self):
        """Return the bottom coordinate of the block."""
        y0 = self._ya
        if self._verticalalignment == 'center':
            y0 -= 0.5 * self._height
        elif self._verticalalignment == 'top':
            y0 -= self._height
        return y0

    def get_xy(self):
        """Return the left and bottom coords of the block as a tuple."""
        return self.get_x(), self.get_y()

    def get_xc(self, ):
        """Return the y coordinate of the block's center."""
        return self.get_x() + 0.5 * self.get_width()

    def get_yc(self, ):
        """Return the y coordinate of the block's center."""
        return self.get_y() + 0.5 * self._height

    def get_center(self,):
        """Return the center point of the block."""
        return (self.get_xc(),
                self.get_yc())

    def get_outflows(self):
        """Return a list of outgoing `._Flows`."""
        return self._outflows

    def get_inflows(self):
        """Return a list of incoming `._Flows`."""
        return self._inflows

    def set_xa(self, xa):
        """Set the x coordinate of the anchor point."""
        self._xa = xa
        self.stale = True

    def set_ya(self, ya):
        """Set the y coordinate of the anchor point."""
        self._ya = ya
        self.stale = True

    def set_y(self, y):
        """
        Set the y coordinate of the block's bottom.

        Note that this method alters the y coordinate of the anchor point.
        """
        self._ya = y
        if self._verticalalignment == 'center':
            self._ya += 0.5 * self._height
        elif self._verticalalignment == 'top':
            self._ya += self._height

    def set_yc(self, yc):
        """
        Set the y coordinate of the block center.

        Note that this method alters the y coordinate of the anchor point.
        """
        self._ya = yc
        if self._verticalalignment == 'bottom':
            self._ya -= 0.5 * self._height
        elif self._verticalalignment == 'top':
            self._ya += 0.5 * self._height

    def set_width(self, width):
        """Set the width of the block."""
        self._artist.set_width(width)

    def set_height(self, height):
        """Set the height of the block."""
        self._height = height
        self.stale = True

    def _set_horizontalalignment(self, align):
        """TODO: write docstring."""
        # TODO: uncomment once in mpl
        # _api.check_in_list(['center', 'left', 'right'], align=align)
        self._horizontalalignment = align
        self.stale = True

    def _set_verticalalignment(self, align):
        """TODO: write docstring."""
        # TODO: uncomment once in mpl
        # _api.check_in_list(['center', 'top', 'bottom'], align=align)
        self._verticalalignment = align
        self.stale = True

    def set_horizontalalignment(self, align):
        """Set the horizontal alignment of the anchor point and the block."""
        self._set_horizontalalignment(self, align)

    def set_verticalalignment(self, align):
        """Set the vertical alignment of the anchor point and the block."""
        self._set_verticalalignment(align)

    def set_outflows(self, outflows):
        """TODO: write docstring."""
        self._outflows = outflows

    def set_inflows(self, inflows):
        """TODO: write docstring."""
        self._inflows = inflows

    # xa = property(get_xa, set_xa, doc="The block anchor's x coordinate")
    # ya = property(get_ya, set_ya, doc="The block anchor's y coordinate")
    # y = property(get_y, set_y, doc="The y coordinate of the block bottom")
    # x = property(get_x, None, doc="The x coordinate of the block bottom")
    inflows = property(get_inflows, set_inflows,
                       doc="List of `._Flow` objects entering the block.")
    outflows = property(get_outflows, set_outflows,
                        doc="List of `._Flow` objects leaving the block.")

    def add_outflow(self, outflow):
        """TODO: write docstring."""
        self._outflows.append(outflow)

    def add_inflow(self, inflow):
        """TODO: write docstring."""
        self._inflows.append(inflow)

    def _create_artist(self, ax, **kwargs):
        """Blocks use :class:`patches.Rectangle` as their patch."""
        self._artist = self._artistcls(self.get_xy(), height=self._height,
                                       axes=ax, **kwargs)

    def add_artist(self, ax):
        """TODO: write docstring."""
        self._artist = ax.add_patch(self._artist)

    def _set_loc_out_flows(self,):
        """TODO: write docstring."""
        yc = self.get_yc()
        for out_flow in self._outflows:
            in_loc = None
            out_loc = None
            if out_flow.target is not None:
                target_inloc = out_flow.target.get_inloc()
                if yc > out_flow.target.get_yc():
                    # draw to top
                    if yc >= target_inloc['top'][1]:
                        # draw from bottom to in top
                        out_loc = 'bottom'
                        in_loc = 'top'
                    else:
                        # draw from top to top
                        out_loc = 'top'
                        in_loc = 'top'
                else:
                    # draw to bottom
                    if yc <= target_inloc['bottom'][1]:
                        # draw from top to bottom
                        out_loc = 'top'
                        in_loc = 'bottom'
                    else:
                        # draw form bottom to bottom
                        out_loc = 'bottom'
                        in_loc = 'bottom'
            else:
                out_flow.out_loc = out_flow.out_flow_vanish
            out_flow.in_loc = in_loc
            out_flow.out_loc = out_loc

    def _sort_out_flows(self,):
        """TODO: write docstring."""
        _top_flows = [
            (i, self._outflows[i])
            for i in range(len(self._outflows))
            if self._outflows[i].out_loc == 'top'
        ]
        _bottom_flows = [
            (i, self._outflows[i])
            for i in range(len(self._outflows))
            if self._outflows[i].out_loc == 'bottom'
        ]
        if _top_flows:
            sorted_top_idx, _flows_top = zip(*sorted(
                _top_flows,
                key=lambda x: x[1].target.get_yc()
                if x[1].target
                # TODO: this should not simply be -10000
                else -10000,
                reverse=True
            ))
        else:
            sorted_top_idx = []
        if _bottom_flows:
            sorted_bottom_idx, _flows_bottom = zip(*sorted(
                _bottom_flows,
                key=lambda x: x[1].target.get_yc()
                if x[1].target
                # TODO: this should not simply be -10000
                else -10000,
                reverse=False
            ))
        else:
            sorted_bottom_idx = []
        sorted_idx = list(sorted_top_idx) + list(sorted_bottom_idx)
        self._outflows = [self._outflows[i] for i in sorted_idx]

    def _sort_in_flows(self,):
        """TODO: write docstring."""
        _top_flows = [
            (i, self._inflows[i])
            for i in range(len(self._inflows))
            if self._inflows[i].in_loc == 'top'
        ]
        _bottom_flows = [
            (i, self._inflows[i])
            for i in range(len(self._inflows))
            if self._inflows[i].in_loc == 'bottom'
        ]
        if _top_flows:
            sorted_top_idx, _flows_top = zip(*sorted(
                _top_flows,
                key=lambda x: x[1].source.get_yc()
                if x[1].source
                # TODO: this should not simply be -10000
                else -10000,
                reverse=True
            ))
        else:
            sorted_top_idx = []
        if _bottom_flows:
            sorted_bottom_idx, _flows_bottom = zip(*sorted(
                _bottom_flows,
                key=lambda x: x[1].source.get_yc()
                if x[1].source
                # TODO: this should not simply be -10000
                else -10000,
                reverse=False
            ))
        else:
            sorted_bottom_idx = []
        sorted_idx = list(sorted_top_idx) + list(sorted_bottom_idx)
        self._inflows = [self._inflows[i] for i in sorted_idx]

    def get_loc_out_flow(self, flow_width, out_loc, in_loc):
        """TODO: write docstring."""
        outloc = self.get_outloc()
        anchor_out = (
            outloc[out_loc][0],
            outloc[out_loc][1] + self.out_margin[out_loc] + (flow_width if in_loc == 'bottom' else 0)
        )
        top_out = (
            outloc[out_loc][0],
            outloc[out_loc][1] + self.out_margin[out_loc] + (flow_width if in_loc == 'top' else 0)
        )
        self.out_margin[out_loc] += flow_width
        return anchor_out, top_out

    def _set_anchor_out_flows(self,):
        """TODO: write docstring."""
        for out_flow in self._outflows:
            out_width = out_flow.flow \
                if out_flow.out_loc == 'bottom' else - out_flow.flow
            out_flow.anchor_out, out_flow.top_out = self.get_loc_out_flow(
                out_width, out_flow.out_loc, out_flow.in_loc
            )

    def _set_anchor_in_flows(self,):
        """TODO: write docstring."""
        for in_flow in self._inflows:
            in_width = in_flow.flow \
                if in_flow.in_loc == 'bottom' else - in_flow.flow
            in_flow.anchor_in, in_flow.top_in = self.get_loc_in_flow(
                in_width, in_flow.out_loc, in_flow.in_loc
            )

    def get_loc_in_flow(self, flow_width, out_loc, in_loc):
        """TODO: write docstring."""
        inloc = self.get_inloc()
        anchor_in = (
            inloc[in_loc][0],
            inloc[in_loc][1] + self.in_margin[in_loc] + (flow_width if out_loc == 'bottom' else 0)
        )
        top_in = (
            inloc[in_loc][0],
            inloc[in_loc][1] + self.in_margin[in_loc] + (flow_width if out_loc == 'top' else 0)
        )
        self.in_margin[in_loc] += flow_width
        return anchor_in, top_in

    def get_inloc(self,):
        """TODO: write docstring."""
        # TODO: dont use dict here.
        x0, y0, width, height = self.get_bounds()
        return {'bottom': (x0, y0),  # left, bottom
                'top': (x0, y0 + height)}  # left, top

    def get_outloc(self,):
        """TODO: write docstring."""
        # _width = self.get_width()
        x0, y0, width, height = self.get_bounds()
        # return {'top': (x0 + _width, y0 + self.get_height()),  # top right
        #         'bottom': (x0 + _width, y0)}  # right, bottom
        return {'top': (x0 + width, y0 + height),  # top right
                'bottom': (x0 + width, y0)}  # right, bottom

    def handle_flows(self,):
        """TODO: write docstring."""
        self._set_loc_out_flows()
        self._sort_in_flows()
        self._sort_out_flows()
        self._set_anchor_in_flows()
        self._set_anchor_out_flows()


class _Flow(_ArtistProxy):
    """
    A connection between two blocks from adjacent columns.
    """
    _artistcls = patches.PathPatch

    def __init__(self, flow, source=None, target=None, label=None, **kwargs):
        """

        Parameters
        -----------
        source: :class:`pyalluv.clusters.Cluster` (default=None)
          Cluster from which the flow originates.
        target: :class:`pyalluv.clusters.Cluster` (default=None)
          Cluster into which the flow leads.

        Other Parameters
        ----------------
        **kwargs : Allowed are all `.Patch` properties:

          %(Patch_kwdoc)s

        Note that `color`, `edgecolor` and `facecolor` also accept the
        particular values `'source'` (or `'s'`), `'target'` (or `'t'`) and
        `'interpolate'`.

        By default *edgecolor* and *facecolor* are set to `lightgray`.
        """
        # self._interp_steps = kwargs.pop('interpolation_steps', 1)
        self.out_flow_vanish = kwargs.pop('out_flow_vanish', 'top')
        self.default_fc = 'gray'
        self.default_ec = 'gray'
        self.default_alpha = 1.0

        super().__init__(label=label, **kwargs)

        self._kwargs = kwargs
        self.source = source
        self.target = target
        self._original_flow = flow
        self.flow = flow
        # attach the flow to the source and target blocks
        if self.source is not None:
            self.source.add_outflow(self)
        if self.target is not None:
            self.target.add_inflow(self)
        self.stale = True

    def _set_from_artist(self, attr, artist):
        """TODO: write docstring."""
        name = '_%s' % attr
        setattr(self._artist, name, getattr(artist, name))

    # TODO: unused > should this be removed?
    def _update_artist_from(self, other):
        """TODO: write docstring."""
        # For some properties we don't need or don't want to go through the
        # getters/setters, so we just copy them directly.
        self._artist._transform = other._transform
        self._artist._transformSet = other._transformSet
        self._artist._visible = other._visible
        self._artist._alpha = other._alpha
        self._artist.clipbox = other.clipbox
        self._artist._clipon = other._clipon
        self._artist._clippath = other._clippath
        # self._label = other._label
        self._artist._sketch = other._sketch
        self._artist._path_effects = other._path_effects
        self._artist.sticky_edges.x[:] = other.sticky_edges.x.copy()
        self._artist.sticky_edges.y[:] = other.sticky_edges.y.copy()
        self._artist.pchanged()
        self._artist._edgecolor = other._edgecolor
        self._artist._facecolor = other._facecolor
        self._artist._original_edgecolor = other._original_edgecolor
        self._artist._original_facecolor = other._original_facecolor
        self._artist._fill = other._fill
        self._artist._hatch = other._hatch
        self._artist._hatch_color = other._hatch_color
        # copy the unscaled dash pattern
        self._artist._us_dashes = other._us_dashes
        self._artist.set_linewidth(other._linewidth)  # also sets dash properties
        self._artist.set_transform(other.get_data_transform())
        # If the transform of other needs further initialization, then it will
        # be the case for this artist too.
        self._artist._transformSet = other.is_transform_set()

    def _get_dimensions(self,):
        """Return the dimensions (width and height) of the linked Proxies."""
        _sx0, _sy0, swidth, sheight = self.source.get_bounds()
        _tx0, _ty0, twidth, theight = self.target.get_bounds()
        return (swidth, sheight), (twidth, theight)

    # TODO: needs updating (no modifications of kwargs)
    def _create_artist(self, ax, **kwargs):
        """TODO: write docstring."""
        _ref_properties = {}
        for coloring in ['edgecolor', 'facecolor', 'color']:
            color = kwargs.pop(coloring, None)

            ###
            # TODO: handle the 'source'/'target'/'interpolate'/'migrate' cases
            # from mpl
            # def get_edgecolor(self):
            #     if cbook._str_equal(self._edgecolors, 'face'):
            #         return self.get_facecolor()
            #     else:
            #         return self._edgecolors
            ###

            if color == 'source':
                _ref_properties[coloring] = self.source
            elif color == 'target':
                _ref_properties[coloring] = self.target
            elif color == 'interpolate':
                # TODO
                _ref_properties[coloring] = None

        (_sw, _sh), (_tw, _th) = self._get_dimensions()
        _dist = None
        if self.out_loc is not None:
            if self.in_loc is not None:
                _dist = 2 / 3 * (self.target.get_inloc()['bottom'][0] -
                                 self.source.get_outloc()['bottom'][0])
            else:
                _dist = 2 * _sw
                # kwargs = _out_kwargs
        else:
            if self.in_loc is not None:
                # kwargs = _in_kwargs
                pass
            else:
                raise Exception('flow with neither source nor target cluster')

        # now complete the path points
        print(self.anchor_out, self.anchor_in)
        if self.anchor_out is not None:
            anchor_out_inner = (self.anchor_out[0] - 0.5 * _sw,
                                self.anchor_out[1])
            dir_out_anchor = (self.anchor_out[0] + _dist, self.anchor_out[1])
        else:
            # TODO set to form vanishing flow
            # anchor_out = anchor_out_inner =
            # dir_out_anchor =
            pass
        if self.top_out is not None:
            top_out_inner = (self.top_out[0] - 0.5 * _sw, self.top_out[1])
            # 2nd point 2/3 of distance between clusters
            dir_out_top = (self.top_out[0] + _dist, self.top_out[1])
        else:
            # TODO set to form vanishing flow
            # top_out = top_out_inner =
            # dir_out_top =
            pass
        if self.anchor_in is not None:
            anchor_in_inner = (self.anchor_in[0] + 0.5 * _tw,
                               self.anchor_in[1])
            dir_in_anchor = (self.anchor_in[0] - _dist, self.anchor_in[1])
        else:
            # TODO set to form new in flow
            # anchor_in = anchor_in_inner =
            # dir_in_anchor =
            pass
        if self.top_in is not None:
            top_in_inner = (self.top_in[0] + 0.5 * _tw, self.top_in[1])
            dir_in_top = (self.top_in[0] - _dist, self.top_in[1])
        else:
            # TODO set to form new in flow
            # top_in = top_in_inner =
            # dir_in_top =
            pass

        vertices = [self.anchor_out, dir_out_anchor, dir_in_anchor,
                    self.anchor_in, anchor_in_inner, top_in_inner, self.top_in,
                    dir_in_top, dir_out_top, self.top_out, top_out_inner,
                    anchor_out_inner]
        # , self.anchor_out]
        codes = [Path.MOVETO, Path.CURVE4, Path.CURVE4, Path.CURVE4,
                 Path.LINETO, Path.LINETO, Path.LINETO, Path.CURVE4,
                 Path.CURVE4, Path.CURVE4, Path.LINETO, Path.LINETO]
        # Path.CLOSEPOLY]
        # TODO: not sure about these values
        closed = True
        readonly = True
        interp_steps = 1
        _path = Path(vertices, codes, interp_steps, closed, readonly)
        self._artist = self._artistcls(_path, axes=ax, **kwargs)
        for prop, ref_artist in _ref_properties.items():
            self._set_from_artist(prop, ref_artist)

    def add_artist(self, ax):
        """TODO: write docstring."""
        self._artist = ax.add_patch(self._artist)


class _ProxyCollection(_ArtistProxy):
    """
    A collection of _ArtistProxy with common styling properties.
    """
    _artistcls = PatchCollection
    _singular_props = ['zorder', 'hatch', 'pickradius', 'capstyle',
                       'joinstyle']

    def __init__(self, proxies, label=None, **kwargs):
        """
        Parameters
        ----------
        blocks : sequence of :obj:`_Block`
            The blocks in this collection.
        label : str, optional
            Label of the collection.
        """
        # TODO: cmap does not work with datetime as x axis yet...
        # get cmap data:
        self._cmap_data = kwargs.pop('cmap_array', None)
        if self._cmap_data is not None:
            # TODO: allow either a block property, x, or custom array
            self._cmap_data = 'x'

        super().__init__(label=label, **kwargs)

        # TODO: this can be more generally just a ArtistProxy
        self._proxies = proxies

    def __iter__(self):
        return iter(self._proxies)

    def __getitem__(self, key):
        return self._proxies[key]

    def to_element_styling(self, styleprops: dict):
        """
        Convert the styling properties to lists matching self._blocks.
        """
        _styprops = dict(styleprops)
        indiv_props = {sp: _styprops.pop(sp)
                       for sp in self._singular_props if sp in _styprops}
        # normalize
        _styprops = cbook.normalize_kwargs(_styprops, self._artistcls)
        for k, v in _styprops.items():
            # if k not in self._singular_props:
            indiv_props[k] = [p.get_styling_prop(k, v)
                              for p in self._proxies]
            # else:
            #    indiv_props[k] = v
        return indiv_props

    def _pre_creation(self, ax=None, **non_artits_props):
        """TODO: write docstring."""
        for proxy in self._proxies:
            proxy.create_artist(ax=ax, **non_artits_props)

    def _create_artist(self, ax, **kwargs):
        """
        Creates `.PatchCollections`s for the blocks in this collection.

        Parameters
        ----------

        """
        match_original = False
        if any(proxy.is_styled for proxy in self._proxies):
            match_original = True
        if match_original:
            kwargs = self.to_element_styling(kwargs)
        # TODO: does this work with 'interpolate'?
        self._artist = self._artistcls(
            [proxy.get_artist() for proxy in self._proxies],
            match_original=match_original,
            **kwargs
        )
        if self._cmap_data is not None:
            self._artist.set_array(
                np.asarray([getattr(proxy, self._cmap_data)
                            for proxy in self._proxies]))

    def add_artist_to_axes(self, ax):
        """Adding the artist to an `~.axes.Axes`."""
        self._artist = ax.add_collection(self._artist)

    def add_proxy(self, proxy):
        """Add a Proxy."""
        self._proxies.append(proxy)


class Tag:
    """
    A collection of `Blocks`
    """
    def __init__(self, label=None, **kwargs):
        """
        Parameters
        ----------
        label : str, optional
            The label of this collection.
        """
        self._kwargs = dict(kwargs)
        self._cmap = self._kwargs.pop('cmap', None)
        self._cmap_data = kwargs.pop('cmap_array', None)
        self._A = {}
        # cm.ScalarMappable.__init__(self, norm, cmap)
        self.label = label
        self._marked_obj = WeakValueDictionary()
        self._closed = False
    # # from collection
    # def update_scalarmappable(self):
    #     """Update colors from the scalar mappable array, if it is not None."""
    #     if self._A is None:
    #         return
    #     # QuadMesh can map 2d arrays (but pcolormesh supplies 1d array)
    #     if self._A.ndim > 1 and not isinstance(self, QuadMesh):
    #         raise ValueError('Collections can only map rank 1 arrays')
    #     if not self._check_update("array"):
    #         return
    #     if np.iterable(self._alpha):
    #         if self._alpha.size != self._A.size:
    #             raise ValueError(f'Data array shape, {self._A.shape} '
    #                              'is incompatible with alpha array shape, '
    #                              f'{self._alpha.shape}. '
    #                              'This can occur with the deprecated '
    #                              'behavior of the "flat" shading option, '
    #                              'in which a row and/or column of the data '
    #                              'array is dropped.')
    #         # pcolormesh, scatter, maybe others flatten their _A
    #         self._alpha = self._alpha.reshape(self._A.shape)

    #     if self._is_filled:
    #         self._facecolors = self.to_rgba(self._A, self._alpha)
    #     elif self._is_stroked:
    #         self._edgecolors = self.to_rgba(self._A, self._alpha)

    def register_proxy(self, proxy):
        """TODO: write docstring."""
        if self._closed:
            # TODO: a closed tag can no longer be used to tag a proxy
            raise Exception()
        proxy_id = id(proxy)
        if proxy_id in self._marked_obj:
            # if the proxy was already registered, ignore it
            return
        # TODO: get the relevant value from the proxy
        # self._kwargs.update(kw)
        if self._cmap_data is not None:
            self._A[proxy_id] = getattr(proxy, self._cmap_data)
        # remember the proxy
        self._marked_obj[proxy_id] = proxy

    def deregister_proxy(self, proxy):
        proxy_id = id(proxy)
        self._marked_obj.pop(id(proxy))
        self._A.pop(proxy_id)

    def _prepare_props(self):
        self._proxy_props = {}
        if self._cmap:
            # create norm
            # map _A to color -> proxi_id to color/lw in self._proxy_props
            for proxy_id in self._marked_obj:
                proxy_prop = {}
                # TODO: add facecolor
                self._proxy_props[proxy_id] = proxy_prop

    def close(self):
        """This method closes the tag for registration."""
        self._prepare_props()
        self._closed = True

    @property
    def is_closed(self,):
        return self._closed

    def get_props(self, proxy_id):
        """TODO: write docstring."""
        if not self._closed:
            self.close()
        # props = cbook.normalize_kwargs(self._kwargs, proxy._artistcls)
        # if self._cmap is not None:
        #     fc = self._get_
        props = dict(self._kwargs)
        props.update(self._proxy_props.get(proxy_id, {}))
        return props


class SubDiagram:
    """
    A collection of Blocks and Flows belonging to a diagram.

    """
    def __init__(self, x, columns, flows, label=None, yoff=0, hspace=1,
                 hspace_combine='add', label_margin=(0, 0), layout='centered',
                 blockprops=None, flowprops=None, **kwargs):
        """
        Parameters
        ----------
        x : sequence of scalars
            A sequence of M scalars that determine the x coordinates of columns
            provided in *columns*.
        columns : sequence of array_like objects
            Sequence of M array-like objects each containing the blocks of a
            column.
            Allowed are `_Block` objects or floats that will be interpreted as
            the size of a block.
        flows : sequence of array_like objects
            ... *TODO*
        label : str, optional
            Label of the diagram.
        yoff : int or float, default: 0
            A constant vertical offset applied to the added diagram.
        hspace : float, (default=1)
            The height reserved for space between blocks expressed as a
            float in the same unit as the block heights.
        hspace_combine : {'add', 'divide'}, default: 'add'
            Set how the vertical space between blocks should be combined.
            If set to 'add' (default) the space between two blocks takes
            the value provided by *hspace*. If set to 'divide' then the sum of
            all spaces between the blocks in a column is set to be equal to
            *hspace*.
        label_margin : tuple, optional
            determine the offset in points for the label.

            .. TODO:
                This should be in points.
        layout : sequence or str, default: 'centered'
            The type of layout used to display the diagram.
            Allowed layouts are: {'centered', 'bottom', 'top', 'optimized'}.

            If as sequence is provided, the M elements must specify the layout
            for each of the M columns in the diagram.

            The following options are available:

            - 'centered' (default): The bigger the block (in terms of height)
              the more it is moved towards the center.
            - 'bottom': Blocks are sorted according to their height with the
              biggest blocks at the bottom.
            - 'top': Blocks are sorted according to their height with the
              biggest blocks at the top.
            - 'optimized': Starting from a centered layout the order of bocks
              in a column is iteratively changed to decrease the vertical
              displacement of all flows attached to the column.

        Other Parameters (TODO)
        ----------------
        **kwargs : Allowed are `.Collection` properties for Blocks and Flows
            Define the styling to apply to all elements in this subdiagram:

            %(Collection_kwdoc)s

        Note that *x* and *columns* must be sequences of the same length.
        """
        self._x = _to_valid_arrays(x, 'x')
        self._yoff = yoff
        # Note: both _block-/_flowprops must be normalized already
        self._blockprops = blockprops or dict()
        self._flowprops = flowprops or dict()

        # create the columns of Blocks
        columns = list(columns)
        _provided_blocks = False
        for col in columns:
            if len(col):
                if isinstance(col[0], _Block):
                    _provided_blocks = True
                break
        _blocks = []
        self._columns = []
        if _provided_blocks:
            for col in columns:
                column = list(col)
                _blocks.extend(column)
                self._columns.append(column)
        else:
            for xi, col in zip(x, columns):
                column = [_Block(size, xa=xi)
                          for size in col]
                self._columns.append(column)
                _blocks.extend(column)
        self._nbr_columns = len(self._columns)

        # TODO: below attributes need to be handled
        self._redistribute_vertically = 4
        self._ylim = None

        # TODO: update blockprops with kwargs? at least handle label
        self._blocks = _ProxyCollection(_blocks, label=label,
                                        **self._blockprops)

        # create the Flows is only based on *flows* and *extout*'s
        _flows = []
        # connect source and target:
        for m, flowM in enumerate(flows):
            # m is the source column, m+1 the target column
            s_col = self._columns[m]
            t_col = self._columns[m + 1]
            for i, row in enumerate(flowM):
                # i is the index of the target block
                for j, flow in enumerate(row):
                    # j is the index of the source block
                    # TODO: pass kwargs?
                    if flow:
                        _flows.append(_Flow(flow=flow, source=s_col[j],
                                            target=t_col[i]))
        # TODO: update flowprops with kwargs and label
        self._flows = _ProxyCollection(_flows, label=label, **self._flowprops)
        self._hspace = hspace
        # TODO: create set_... and process like set_hspace
        self._hspace_combine = hspace_combine
        self.set_layout(layout)
        self._label_margin = label_margin
        self._kwargs = cbook.normalize_kwargs(kwargs,
                                              _ProxyCollection._artistcls)
        self.stale = True

    def __iter__(self):
        return iter(self._columns)

    def __getitem__(self, key):
        return self._columns[key]

    def _update_datalim(self):
        """Return the limits of the block collection in data units."""
        # TODO: set x margin (for now just 1%)
        xmin, xmax = min(self._x), max(self._x)
        # setting some initial y limits
        for _col in self._columns:
            if _col:
                y0, height = _col[0].get_ylim()
                ymin, ymax = y0, y0 + height
                break
        # getting x limits
        for _block in self._columns[0]:
            x0, width = _block.get_xlim()
            xmin = min(xmin, x0)
        for _block in self._columns[-1]:
            x0, width = _block.get_xlim()
            xmax = max(xmax, x0 + width)
        # getting y limits
        for _col in self._columns:
            if _col:
                y0, height = _col[0].get_ylim()
                ymin = min(ymin, y0)
                y0, height = _col[-1].get_ylim()
                ymax = max(ymax, y0 + height)
        self._xlim = xmin, xmax
        self._ylim = ymin, ymax

    def get_blocks(self):
        return self._blocks

    def get_datalim(self,):
        """Returns the limits in data units (x0, y0, x1, y1)."""
        if self.stale:
            self.generate_layout()
        x0, x1 = self._xlim
        y0, y1 = self._ylim
        return x0, y0, x1, y1

    def get_visuallim(self):
        """Returns the data limit with sensible margins added."""
        xmin, ymin, xmax, ymax = self.get_datalim()
        # TODO: set x margin (for now just 1%)
        xmargin = 0.01 * (max(self._x) - min(self._x))
        if self._hspace_combine == 'add':
            ymargin = self._hspace
        else:
            ymargin = self._hspace / max(len(col) for col in self._columns)
        return xmin - xmargin, ymin - ymargin, xmax + xmargin, ymax + ymargin

    def get_layout(self):
        """Get the layout of this diagram"""
        return self._layout

    def get_columns(self,):
        """Get all columns of this subdiagram"""
        if self.stale:
            self.generate_layout()
        return self._columns

    def get_column(self, col_id):
        """TODO: write docstring."""
        return self._columns[col_id]

    def get_block(self, identifier):
        """TODO: write docstring."""
        if isinstance(identifier, int):
            return self._blocks[identifier]
        else:
            col_id, block_id = identifier
            return self._columns[col_id][block_id]

    def get_x(self):
        """Get the horizontal positioning of the columns"""
        return self._x

    def set_column_layout(self, col_id, layout):
        """Set the layout for a single column"""
        # TODO: uncomment once in mpl
        # _api.check_in_list(['centered', 'top', 'bottom', 'optimized'],
        #                    layout=layout)
        self._layout[col_id] = layout

    def get_column_layout(self, col_id):
        """Get the layout of a single column."""
        return self._layout[col_id]

    def set_layout(self, layout):
        """Set the layout for this diagram"""
        if isinstance(layout, str):
            # TODO: uncomment once in mpl
            # _api.check_in_list(['centered', 'top', 'bottom', 'optimized'],
            #                    layout=layout)
            self._layout = [layout for _ in range(self._nbr_columns)]
        else:
            self._layout = []
            for _layout in layout:
                # TODO: uncomment once in mpl
                # _api.check_in_list(['centered', 'top', 'bottom', 'optimized'],
                #                    layout=layout)
                self._layout.append(_layout)
        self.stale = True

    def generate_layout(self, ):
        """TODO: write docstring."""
        for col_id in range(self._nbr_columns):
            # TODO: handle the layout parameter
            self._distribute_blocks(col_id)
        # TODO: move columns vert. to minimize vertical comp. flows attached
        self.stale = False

    def add_block(self, column: int, block):
        """Add a Block to a column."""
        self._blocks.add_proxy(block)
        self._columns[column].append(block)
        self.stale = True

    def add_flow(self, column, flow):
        """TODO: write docstring."""
        # TODO: _columns can only contain indices for blocks
        # self._columns[column].append(flow)
        self.stale = True
        pass

    def get_column_hspace(self, col_id):
        """TODO: write docstring."""
        if self._hspace_combine == 'add':
            return self._hspace
        else:
            nbr_blocks = len(self._columns[col_id])
            if nbr_blocks > 1:
                return self._hspace / (nbr_blocks - 1)
            else:
                return 0

    def _distribute_blocks(self, col_id: int):
        """
        Distribute the blocks in a column.

        Parameters
        -----------
        col_id: int
            The index of the column to recompute the distribution of blocks.

        Returns
        -------
        tuple
            (y_min, y_max) of the column in data units

        """
        nbr_blocks = len(self._columns[col_id])
        layout = self.get_column_layout(col_id)
        col_hspace = self.get_column_hspace(col_id)
        if nbr_blocks:
            # sort clusters according to height
            ordering, _column = zip(
                *sorted(enumerate(self._columns[col_id]),
                        key=lambda x: x[1].get_height())
            )
            if layout == 'top':
                # TODO: do the reordering outside if/elif/else (after)
                self._reorder_column(col_id, ordering)
                self._update_ycoords(col_id, col_hspace, layout)
            elif layout == 'bottom':
                ordering = ordering[::-1]
                self._reorder_column(col_id, ordering)
                self._update_ycoords(col_id, col_hspace, layout)
            # in both cases no further sorting is needed
            else:
                # sort so to put biggest height in the middle
                ordering = ordering[::-2][::-1] + \
                    ordering[nbr_blocks % 2::2][::-1]
                # update the ordering the update the y coords
                self._reorder_column(col_id, ordering)
                self._update_ycoords(col_id, col_hspace, layout)
                # ###
                # TODO: both methods below need to be checked
                if layout == 'optimized':
                    # # now sort again considering the flows.
                    # self._decrease_flow_distances(col_id)
                    # # perform pairwise swapping for backwards flows
                    # self._pairwise_swapping(col_id)
                    raise NotImplementedError("The optimized layout is not yet"
                                              " implemented")
            # _min_y = min(self._columns[col_id],
            #              key=lambda x: x.get_y()).get_y() - 2 * col_hspace
            # _max_y_cluster = max(self._columns[col_id],
            #                      key=lambda x: x.get_y() + x.get_height())
            # _max_y = _max_y_cluster.get_y() + \
            #     _max_y_cluster.get_height() + 2 * col_hspace
            # self._ymin = min(self._ymin,
            #                  _min_y) if self._ymin is not None else _min_y
            # self._ymax = max(self._ymax,
            #                  _max_y) if self._ymax is not None else _max_y

    def _decrease_flow_distances(self, col_id):
        """TODO: write docstring."""
        _column = self._columns[col_id]
        # TODO: does not really make sense to recompute them here
        nbr_blocks = len(_column)
        layout = self.get_column_layout(col_id)
        col_hspace = self.get_column_hspace(col_id)
        old_mid_heights = [block.get_yc() for block in _column]
        # do the redistribution a certain amount of times
        _redistribute = False
        for _ in range(self._redistribute_vertically):
            # TODO: check this as soon as Flows are set-up correctly
            for block in _column:
                weights = []
                positions = []
                for in_flow in block.inflows:
                    if in_flow.source is not None:
                        weights.append(in_flow.flow)
                        positions.append(in_flow.source.get_yc())
                if sum(weights) > 0.0:
                    _redistribute = True
                    block.set_yc(sum([
                        weights[i] * positions[i]
                        for i in range(len(weights))
                    ]) / sum(weights))
            if _redistribute:
                sort_key = [bisect_left(old_mid_heights, col.get_yc())
                            for col in _column]
                cs, _sort_key = zip(
                    *sorted(zip(list(range(nbr_blocks)), sort_key,),
                            key=lambda x: x[1])
                )
                self._reorder_column(col_id, ordering=cs)
                # redistribute them
                self._update_ycoords(col_id, col_hspace, layout)
                old_mid_heights = [block.get_yc()
                                   for block in self._columns[col_id]]
            else:
                break

    def _pairwise_swapping(self, col_id):
        """TODO: write docstring."""
        # TODO: this is broken: update the ordering then call _reorder_column
        _column = self._columns[col_id]
        nbr_blocks = len(_column)
        col_hspace = self.get_column_hspace(col_id)
        for _ in range(int(0.5 * nbr_blocks)):
            for i in range(1, nbr_blocks):
                b1, b2 = _column[i - 1], _column[i]
                if self._swap_blocks((b1, b2), col_hspace, 'backwards'):
                    b2.set_y(b1.get_y())
                    b1.set_y(b2.get_y() + b2.get_height() + col_hspace)
                    _column[i - 1], _column[i] = b2, b1
        for _ in range(int(0.5 * nbr_blocks)):
            for i in range(1, nbr_blocks):
                b1 = _column[nbr_blocks - i - 1]
                b2 = _column[nbr_blocks - i]
                if self._swap_blocks((b1, b2), col_hspace, 'backwards'):
                    b2.set_y(b1.get_y())
                    b1.set_y(b2.get_y() + b2.get_height() + col_hspace)
                    _column[nbr_blocks - i - 1] = b2
                    _column[nbr_blocks - i] = b1
        self.stale = True

    def _reorder_column(self, col_id, ordering):
        """Update the ordering of blocks in a column"""
        _column = self._columns[col_id]
        self._columns[col_id] = [_column[newid] for newid in ordering]
        self.stale = True

    def _update_ycoords(self, column: int, hspace, layout):
        """
        Update the y coordinate of the blocks in a column based on the
        diagrams vertical offset, the layout chosen for this column and the
        order of the blocks.

        Parameters
        ----------
        column : int
            Index of the column to reorder.
        """
        displace = self._yoff
        _column = self._columns[column]
        for block in _column:
            block.set_y(displace)
            displace += block.get_height() + hspace
        # now offset to center
        low = _column[0].get_y()  # this is just self._yoff
        # this is just `displace`:
        high = _column[-1].get_y() + _column[-1].get_height()

        if layout == 'centered' or layout == 'optimized':
            _offset = 0.5 * (high - low)
        elif layout == 'top':
            _offset = (high - low)
        else:
            _offset = 0
        if _offset:
            for block in _column:
                block.set_y(block.get_y() - _offset)
        self.stale = True

    def _swap_blocks(self, blocks, hspace, direction='backwards'):
        """
        Check if swapping to blocks leads to shorter vertical flow distances.
        """
        squared_diff = {}
        for block in blocks:
            weights, sqdiff = [], []
            if direction in ['both', 'backwards']:
                for flow in block.inflows:
                    if flow.source is not None:
                        weights.append(flow.flow)
                        sqdiff.append(abs(block.get_yc() - flow.source.get_yc()))
            if direction in ['both', 'forwards']:
                for flow in block.outflows:
                    if flow.target is not None:
                        weights.append(flow.flow)
                        sqdiff.append(abs(block.get_yc() - flow.target.get_yc()))
            if sum(weights) > 0.0:
                squared_diff[block] = sum(
                    [weights[i] * sqdiff[i] for i in range(len(weights))]
                ) / sum(weights)
        # assert n1.get_y() < n2.get_y()
        # TODO: Cannot recreate the thought process behind this...
        inv_mid_height = [blocks[0].get_y() + blocks[1].get_height() + hspace + 0.5 * blocks[0].get_height(),
                          blocks[0].get_y() + 0.5 * blocks[1].get_height()]
        squared_diff_inf = {}
        for i, block in enumerate(blocks):
            weights = []
            sqdiff = []
            if direction in ['both', 'backwards']:
                for flow in block.inflows:
                    if flow.source is not None:
                        weights.append(flow.flow)
                        sqdiff.append(abs(
                            inv_mid_height[i] - flow.source.get_yc()
                        ))
            if direction in ['both', 'forwards']:
                for flow in block.outflows:
                    if flow.target is not None:
                        weights.append(flow.flow)
                        sqdiff.append(
                            abs(inv_mid_height[i] - flow.target.get_yc())
                        )
            if sum(weights) > 0.0:
                squared_diff_inf[block] = sum(
                    [weights[i] * sqdiff[i] for i in range(len(weights))]
                ) / sum(weights)
        if sum(squared_diff.values()) > sum(squared_diff_inf.values()):
            return True
        else:
            return False

    # TODO: is this still needed? if yes, automate or use _singular_props
    @classmethod
    def separate_kwargs(cls, kwargs):
        """Separate all relevant kwargs for the init if a SubDiagram."""
        sdkwargs, other_kwargs = dict(), dict()
        sd_args = ['x', 'columns', 'match_original', 'yoff', 'layout',
                   'hspace_combine', 'label_margin', 'cmap', 'norm',
                   'cmap_array', 'blockprops', 'flowprops']
        for k, v in kwargs.items():
            if k in sd_args:
                sdkwargs[k] = v
            else:
                other_kwargs[k] = v
        return sdkwargs, other_kwargs

    def create_artists(self, ax, **kwargs):
        """TODO: write docstring."""
        if self.stale:
            self.generate_layout()
        _kwargs = dict(kwargs)
        _kwargs.update(self._kwargs)
        _blockkws = cbook.normalize_kwargs(_kwargs, self._blocks._artistcls)
        self._blocks.create_artist(ax=ax, **_blockkws)
        self._blocks.add_artist_to_axes(ax)
        for block in self._blocks:
            block.handle_flows()
        _flowkws = cbook.normalize_kwargs(_kwargs, self._flows._artistcls)
        self._flows.create_artist(ax=ax, **_flowkws)
        self._flows.add_artist_to_axes(ax)
        # at last make sure the datalimits are updated
        self._update_datalim()


class Alluvial:
    """
    Alluvial diagram.

        Alluvial diagrams are a variant of flow diagram designed to represent
        changes in classifications, in particular changes in network
        structure over time.
        `Wikipedia (23/1/2021) <https://en.wikipedia.org/wiki/Alluvial_diagram>`_
    """
    # @docstring.dedent_interpd
    def __init__(self, x=None, ax=None, tags=None, blockprops=None,
                 flowprops=None, label_kwargs={}, **kwargs):
        """
        Create a new Alluvial instance.


        Parameters
        ===========
        x : array-like, optional
          The x coordinates for the columns in the Alluvial diagram.

          They will be used as default coordinates whenever a sub-diagram is
          added that does not specify it's own x coordinates.

          If not given, the x coordinates will be inferred as soon as the first
          diagram is added and default to the range of the number of columns in
          the diagram.

        ax: `~.axes.Axes`
          Axes onto which the Alluvial diagram should be drawn.
          If *ax* is not provided a new Axes instance will be created.
        cluster_w_spacing: float, int (default=1)
          Vertical spacing between blocks.
        blockprops : dict, optional
          The properties used to draw the blocks. *blockprops* accepts all
          arguments for :class:`matplotlib.patches.Rectangle`:

          %(Rectangle_kwdoc)s

        flowprops: dict, optional
          The properties used to draw the flows. *flowprops* accepts keyword
          arguments for :class:`matplotlib.patches.PathPatch`:

          %(Patch_kwdoc)s

          TODO: this is old and needs to be redone:
          Note
          -----

            Passing a string to `facecolor` and/or `edgecolor` allows to color
            flows relative to the color of their source or target blocks.

            ``'source'`` or ``'target'``:
              will set the facecolor equal to the color of the respective block.

              ``'cluster'`` *and* ``'source'`` *are equivalent.*

            ``'<cluster>_reside'`` or ``'<cluster>_migration'``:
              set the color based on whether source and target block have the
              same color or not. ``'<cluster>'`` should be either
              ``'source'`` or ``'target'`` and determines the
              block from which the color is taken.

              **Examples:**

              ``facecolor='cluster_reside'``
                set `facecolor` to the color of the source block if both source
                and target block are of the same color.

              ``edgecolor='cluster_migration'``
                set `edgecolor` to the color of the source block if source and
                target block are of different colors.

            TODO: check the args below, remove or add to init
            fill_figure: bool
              indicating whether or not set the
              axis dimension to fill up the entire figure
            invisible_x/invisible_y: bool
              whether or not to draw these axis.
            redistribute_vertically: int (default=4)
              how often the vertical pairwise swapping of blocks at a given
              time point should be performed.

          Note that *blockprops* and *flowprops* set the properties of all
          sub-diagrams, unless specific properties are provided when a
          sub-diagram is added (see :meth:`add` for details), or
          :meth:`set_blockprops` is called before adding further sub-diagrams.

        """
        if x is not None:
            self._x = _to_valid_arrays(x, 'x')
        else:
            self._x = None
        # create axes if not provided
        if ax is None:
            import matplotlib.pyplot as plt
            fig = plt.figure()
            # TODO: not sure if specifying the ticks is necessary
            ax = fig.add_subplot(1, 1, 1, yticks=[])
        self.ax = ax
        # store normalized styling properties
        self._blockprops = cbook.normalize_kwargs(blockprops,
                                                  _ProxyCollection._artistcls)
        self._flowprops = cbook.normalize_kwargs(flowprops,
                                                 _ProxyCollection._artistcls)
        # nothing to set for blocks for now
        # self._inject_default_blockprops()
        self._inject_default_flowprops()
        # there is no imminent reason why to keep the original input, but...
        self._original_blockprops = blockprops
        self._original_flowprops = flowprops
        self._diagrams = []
        self._tags = dict()
        self._extouts = []
        self._diagc = 0
        self._dlabels = []
        self._xlim = None
        self._ylim = None
        # how many x ticks are maximally shown
        self.max_nbr_xticks = 10
        self._defaults = dict()
        _kwargs = cbook.normalize_kwargs(kwargs,
                                         _ProxyCollection._artistcls)
        fc = _kwargs.get('facecolor', None)
        if fc is None:
            self._color_cycler = itertools.cycle(
                mpl.rcParams['axes.prop_cycle'])
        else:
            # Note passing rgb/rgba arrays is not supported
            self._color_cycler = itertools.cycle(cycler(color=fc))
        # TODO: if arguments for add are passed they cannot remain in kwargs
        if _kwargs:
            # ###
            # TODO: kw separation should be a method of SubDiagram entirely
            flows = _kwargs.pop('flows', None)
            ext = _kwargs.pop('ext', None)
            label = _kwargs.pop('label', '')
            fractionflow = _kwargs.pop('fractionflow', False)
            tags = _kwargs.pop('tags', None)
            # draw a diagram if *flows* are provided
            sdkw, self._defaults = SubDiagram.separate_kwargs(_kwargs)
            # ###
            if flows is not None or ext is not None:
                self.add(flows=flows, ext=ext, extout=None, x=self._x,
                         label=label, yoff=0,
                         fractionflow=fractionflow, tags=tags,
                         **sdkw)
                self.finish()

    def get_x(self):
        """Return the sequence of x coordinates of the Alluvial diagram"""
        return self._x

    def set_x(self, x):
        """
        Set the sequence of x coordinates for all columns in the Alluvial
        diagram.

        Parameters
        ----------
        x : sequence of scalars
            Sequence of M scalars setting the x coordinates for all columns in
            all subdiagrams.

        Note that setting the coordinates will have no effect on subdiagrams
        that were already added. Only further calls of :meth:`add` will use the
        new x coordinates as default horizontal positioning.
        """
        if x is None:
            self._x = None
        else:
            self._x = _to_valid_arrays(x, 'x')

    def get_diagrams(self):
        """Get all sub-diagrams."""
        return self._diagrams

    def get_diagram(self, diag_id):
        """TODO: write docstring."""
        return self._diagrams[diag_id]

    def _to_cols_and_flows(self, cinit, flows, ext, extout, fractionflow):
        """
        Create the columns of an alluvial diagram and convert fractional flows
        to absolute quantities.
        """
        # create the columns
        columns = [cinit]
        if flows is not None:
            flows = np.copy(flows)
            for i in range(len(flows)):
                flow = flows[i]
                e = ext[i + 1]
                if len(flow):
                    if fractionflow:
                        _flow = flow.dot(columns[-1])
                        # create the absolute flow matrix
                        flows[i] = flow * columns[-1]
                    else:
                        _flow = flow.sum(1)
                    _col = _flow + e
                else:
                    _col = e
                columns.append(_col)
        if extout is not None:
            pass  # TODO: check extout format
        return columns, flows

    def add_memberships(self, memberships, absentval=None, x=None, label=None,
                        yoff=None, **kwargs):
        """TODO: write docstring."""
        return self._from_membership(memberships, absentval, x, label, yoff,
                                     **kwargs)

    # TODO: rename this to the above
    # TODO: add support for changing axis=0/1 in case of pandas df
    def _from_membership(self, memberships, absentval=None, x=None, label=None,
                         yoff=None, **kwargs):
        memberships = _to_valid_arrays(memberships, 'memberships', np.int)
        for i, membership in enumerate(memberships):
            if np.unique(membership).size != np.max(membership) + 1:
                raise ValueError("The provided membership lists must associate"
                                 " nodes to groups that are continuously"
                                 " numbered starting from 0. This is not the"
                                 f" case at index {i}:\n{membership}")
        # TODO: make it work with dataframe
        # if not isinstance(memberships, (list, tuple)):
        #     try:
        #         memberships.index.values
        #     except AttributeError:
        #         memberships.shape
        #         # return np.arange(y.shape[0], dtype=float), y
        #         pass
        columns, flows = [], []
        # process the first membership list
        memship_last = memberships[0]
        nbr_blocks_last, col = _memship_to_column(memship_last, absentval)
        columns, flows = [col], []
        for i in range(1, len(memberships)):
            # create the flow matrix
            nbr_blocks, col = _memship_to_column(memberships[i])
            flow_dims = (nbr_blocks, nbr_blocks_last)
            flow, ext = _between_memships_flow(flow_dims, memship_last,
                                               memberships[i])
            flows.append(flow)
            # add ext to the column and append to the columns
            columns.append(col + ext)
            memship_last = memberships[i]
            nbr_blocks_last = nbr_blocks
        if x is not None:
            x = _to_valid_arrays(x, 'x')
        elif self._x is not None:
            x = self._x
        else:
            x, columns = index_of(columns)
        x, columns = self._determine_x(x, columns)
        return self._add(columns, flows, x=x, label=label, yoff=yoff, **kwargs)

    @classmethod
    def from_memberships(cls, memberships, absentval=None, x=None, label=None,
                         yoff=0.0, **kwargs):
        """
        Add an new subdiagram from a sequence of membership lists.

        Parameters
        ----------
        memberships : sequence of array-like objects or dataframe
            The length of the sequence determines the number of columns in the
            diagram. Each element in the sequence must be an array-like object
            representing a membership list. For further details see below.
        absentval : int or np.nan (default=None)
            Notes for which  this value is encountered in the membership lists
            are considered to not be present.

        Note that all elements in *memberships* must be membership lists of
        identical length. Further the group identifiers present in a membership
        list must be derived from an enumeration of the groups.

        """
        alluvial = Alluvial(x=x)
        alluvial._from_membership(memberships, absentval, label=label,
                                  yoff=yoff, **kwargs)
        return alluvial

    def _inject_default_blockprops(self,):
        """Completing styling properties of blocks with sensible defaults."""
        pass

    def _inject_default_flowprops(self,):
        """Completing styling properties of flows with sensible defaults."""
        self._flowprops['alpha'] = self._flowprops.get('alpha', 0.7)

    def get_defaults(self,):
        """TODO: write docstring."""
        self._defaults['facecolor'] = next(self._color_cycler)['color']
        return self._defaults

    def _add(self, columns, flows, x, label, yoff, tags=None, **kwargs):
        """TODO: write docstring."""
        # TODO: handle tags: not actually tags, but str to define ensembles
        # like 'columns', 'index', etc. might also get rid of this.
        _kwargs = cbook.normalize_kwargs(kwargs,
                                         _ProxyCollection._artistcls)
        _blockprops = _kwargs.pop('blockprops', self._blockprops)
        _flowprops = _kwargs.pop('flowprops', self._flowprops)
        diagram = SubDiagram(x=x, columns=columns, flows=flows, label=label,
                             blockprops=_blockprops, flowprops=_flowprops,
                             yoff=yoff, **_kwargs)
        self._add_diagram(diagram)
        self._dlabels.append(label or f'diagram-{self._diagc}')
        self._diagc += 1
        return diagram

    def add(self, flows, ext=None, extout=None, x=None, label=None, yoff=0,
            fractionflow=False, tags=None, **kwargs):
        r"""
        Add an Alluvial diagram with a vertical offset.
        The offset must be provided in the same units as the block sizes.

        Parameters
        ----------
        flows : sequence of array-like objects
            The flows between columns of the Alluvial diagram.

            *flows[i]* determines the flow matrix :math:`\mathbf{M}^i` from
            blocks in column *i* to the blocks in column *i+1*. The entry
            `\mathbf{M}^i_{k,l}` gives the amount that flows from block `l` in
            column *i* to block `k` in column *i+1*.

            Note that an Alluvial diagram with M columns needs *flows* to be
            a sequence of M-1 array-like objects.
        ext : sequence, optional
            External inflow to the Alluvial diagram. Supported formats are:

            - sequence of M array-like objects: Specify for each of the M
              columns in the diagram the inflows to the blocks.
            - sequence of floats: Set the block sizes in the initial column.

            If *ext* is not provided, the block sizes for the initial columns
            are inferred from the first entry in *flows* as the column-wise sum
            of *flows[0]*.
        extout : iterable, optional
            The outflows to blocks belonging to another Alluvial diagram.
            The values provided in *extout* must be of the shape (M-1, N, P),
            with N the number of blocks in the diagram that is added and P the
            number of blocks in the destination diagram to which the flows will
            be directed.

            If a dictionary is provided a key must specify the destination
            diagram. Allowed is either the label, *dlabel*, or the index of a
            diagram.

            If a list is provided the entries are mapped to diagrams by index.
        x : array-like, optional
            The x coordinates of the columns.

            When provided, the added diagram ignores the x coordinates that might
            have been provided on initiation of the `.Alluvial` instance or any
            previous :meth:`.Alluvial.add` call.

            If the `.Alluvial` instance had no values set for the x coordinates
            *x* will be set to the new default.

            If not provided and no x coordinates have been set previously, then
            the x coordinates will default to the range defined by the number
            of columns in the diagram to add.
        label : string, optional
            The label of the diagram to add.
        fractionflow : bool, default: False
            When set to *False* (the default) the values in *flows* are
            considered to be absolute values.

            If set to *True* the values in *flows* are considered to be
            fractions of block sizes, and the actual flow between columns *i*
            and *i+1* is given by the dot product of *flows[i]* and the array
            of block sizes in column *i*.

            If fractions are provided,  you must set *ext* to provide at least
            the block sizes for the initial column of the Alluvial diagram.
        yoff : int or float, default: 0
            A constant vertical offset applied to the added diagram.
        tags : sequence or str, optional
            Tagging of the blocks. Tags can be provided in the following
            formats:

            - String, allowed are {'column', 'index'}.
              If *tags* is set to 'column', all blocks in a column get the same
              tag. If 'index' is used, in each column the blocks is tagged by
              their index in the column.
            - Sequence of M tags, providing for each column a separate tag.
            - Sequence of list of tags, providing fore each block in each
              column a tag.

            If a sequence is provided, the tags can be any hashable object.

            Note that *tags* should be used in combination with *tagprops* in
            order to specify the styling for each tag.
        tagprops : dict, optional
            Provide for each tag a dictionary that specifies the styling of
            blocks with this tag. See :meth:`

        Other Parameters
        ----------------
        **kwargs : `.SubDiagram` properties

            .. TODO:
                get doc from SubDiagram.__init__

        Notes
        -----
        The procedure to set the block sizes of column *i+1* changes depending
        on whether *flows* provides fractions of block sizes (if *fractionflow*
        is set to True) or absolute values (default, *fractionflow* is False).
        For a column *i* with N blocks and the column *i+1* with P blocks, the
        relation is defined as as follows:

        - *fractionflow* is False:

          The block sizes in column *i+1* are given by:

          .. math::
              \textbf{c}_{i+1} = \mathbf{F}_i\cdot\textbf{1}+\textbf{e}_{i+1},

          where :math:`\mathbf{F}_i` is the flow matrix of shape (P, N), given
          by *flow[i]*, :math:`\textbf{1}` is a vector of ones of shape (N) and
          :math:`\textbf{e}_{i+1}` is the external inflow vector of shape (P),
          given by *e[i+1]*.
        - *fractionflow* is True:

          The block sizes in column *i+1* depend directly on the block sizes of
          column *i*, :math:`\textbf{c}_{i}`, and are given by:

          .. math::
              \textbf{c}_{i+1}=\mathbf{F}_i\cdot\textbf{c}_i+\textbf{e}_{i+1},

          where :math:`\mathbf{F}_i` is the flow matrix of shape (P, N), given
          by *flow[i]*, :math:`\textbf{c}_i` the vector of N block sizes in
          column *i* and :math:`\textbf{e}_{i+1}` the external inflow vector of
          shape (P) given by *e[i+1]*.
        """
        # TODO: make sure empty flows are accepted
        if flows is not None:
            nbr_cols = len(flows) + 1
            flows = _to_valid_arrays(flows, attribute='flows')
        else:
            flows = []
            nbr_cols = None
        # check ext and set initial column
        if ext is None:
            if fractionflow:
                raise TypeError("'ext' cannot be None if 'fractionflow' is"
                                " True: You need to provide at least the block"
                                " sizes for the first column of the Alluvial"
                                " diagram if the flows are given as"
                                " fractions.")
            elif nbr_cols is None:
                raise TypeError("'ext' cannot be None if 'flows' is None too."
                                " You need to provide either `flows` or `ext`"
                                " to create an Alluvial diagram.")
            ext = np.zeros(nbr_cols)
            # Note: extout from the first column are ignored
            cinit = flows[0].sum(0)
        else:
            ext = _to_valid_arrays(ext, 'ext')
            if isinstance(ext[0], np.ndarray):
                cinit = ext[0]
                if nbr_cols is None:  # if no flows were provided
                    nbr_cols = len(ext)
                    flows = [[] for _ in range(nbr_cols - 1)]
            else:
                cinit = ext[:]
                if nbr_cols is None:
                    nbr_cols = 1
                ext = np.zeros(nbr_cols)  # Note: we overwrite ext in this case
        columns, flows = self._to_cols_and_flows(cinit, flows, ext, extout,
                                                 fractionflow)

        x, columns = self._determine_x(x, columns)
        # TODO: extout are not processed so far
        self._extouts.append(extout)
        return self._add(columns=columns, flows=flows, x=x, label=label,
                         yoff=yoff, tags=tags, **kwargs)

    def _determine_x(self, x, columns):
        """TODO: write docstring."""
        if x is not None:
            x = _to_valid_arrays(x, 'x')
        elif self._x is not None:
            x = self._x
        else:
            x, columns = index_of(columns)
        return x, columns

    def _add_diagram(self, diagram):
        """
        Add a new subdiagram to the Alluvial diagram.
        """
        self._diagrams.append(diagram)

    def _create_collections(self):
        """TODO: write docstring."""
        combined_x = []
        for diagram in self._diagrams:
            # TODO: Probably should not mess with the zorder, but at least
            # make it a property of Alluvial...
            diag_zorder = 4
            defaults = self.get_defaults()
            diagram.create_artists(ax=self.ax, zorder=diag_zorder,
                                   **defaults)
            combined_x.extend(diagram.get_x().tolist())
            _xmin, _ymin, _xmax, _ymax = diagram.get_visuallim()
            self._xlim = _update_limits(self._xlim, _xmin, _xmax)
            self._ylim = _update_limits(self._ylim, _ymin, _ymax)

    def _collect_x_positions(self):
        """Get the x coordinates of the columns in all sub-diagrams."""
        combined_x = []
        for diagram in self._diagrams:
            combined_x.extend(diagram.get_x().tolist())
        return sorted(set(combined_x))

    def finish(self,):
        """Draw the Alluvial diagram."""
        self._create_collections()
        # do some styling of the axes
        # TODO: make this a function of the layout
        self.ax.xaxis.set_ticks_position('bottom')
        x_positions = self._collect_x_positions()
        self.ax.xaxis.set_major_locator(
            mticker.FixedLocator(x_positions, self.max_nbr_xticks - 1)
        )
        self.ax.set_xlim(*self._xlim)
        self.ax.set_ylim(*self._ylim)

    # TODO uncomment once in mpl
    # @docstring.dedent_interpd
    def register_tag(self, label, **kwargs):
        """
        Register a new tag.

        Parameters
        ----------
        tag : Any
            A hashable object used as identifier for the tag.

        Other Parameters
        ----------------
        **kwargs : `.Collection` properties
            Define the styling to apply to all blocks with this tag:

            %(Collection_kwdoc)s

        Note that if the tag has already been registered, a warning message is
        issued and the call will have no effect on the existing tag. If you
        want to update the styling of an existing tag, use :meth:`update_tag`
        instead.
        """
        if label in self._tags:
            _log.warning(
                f"The tag '{label}' was already registered. Registering an"
                " existing tag again has no effect. You must use *update_tag*"
                "if you want to change the styling of an existing tag."
            )
            return None
        self._tags[label] = Tag(label=label, **kwargs)
        return self._tags[label]

    def tag_blocks(self, tag, *args):
        """
        Tagging a selection of blocks.

        TODO: description of selection procedure passing slices to args.
        """
        if isinstance(tag, str):
            tag = self._tags[tag]
        if tag.is_closed:
            # TODO: add some info to why this tag might be closed
            raise Exception('A closed tag cannot be used on tag blocks')
        nbr_args = len(args)
        # convert all input to slices
        
        def _to_slice(arg):
            if isinstance(arg, int):
                return slice(arg, arg + 1)
            elif isinstance(arg, slice):
                return arg
            elif arg is None:
                return slice(None, None)
            else:
                return slice(*arg)
        args = [_to_slice(arg) for arg in args]
        if nbr_args == 1:
            _subdselect = slice(None, None)
            _colselect = slice(None, None)
            _bselect = args[0]
        elif nbr_args == 2:
            _subdselect = slice(None, None)
            _colselect = args[0]
            _bselect = args[1]
        elif nbr_args == 3:
            _subdselect = args[0]
            _colselect = args[1]
            _bselect = args[2]
        for diagram in self._diagrams[_subdselect]:
            for col in diagram[_colselect]:
                for block in col[_bselect]:
                    block.add_tag(tag)

    def update_tag(self, label, **kwargs):
        """TODO: write docstring."""
        if label not in self._tags:
            _log.warning(
                f"The tag '{label}' is not registered.You must"
                " register it first with *register_tag*."
            )
            return None
        tag = self._tags[label]
        tag.update(kwargs)


# TODO: legend handler for subdiagram and for alluvial diagram
class AlluvialHandler:
    def legend_artist(self, legend, orig_handle, fontsize, handlebox):
        x0, y0 = handlebox.xdescent, handlebox.ydescent
        width, height = handlebox.width, handlebox.height
        # TODO: construct a simple alluvial diag
        patch = _Block(height=height, xa=x0, ya=y0, width=width, fc='red',
                       transform=handlebox.get_transform())
        # patch = mpatches.Rectangle([x0, y0], width, height, facecolor='red',
        #                            edgecolor='black', hatch='xx', lw=3,
        #                            transform=handlebox.get_transform())
        handlebox.add_artist(patch)
        return patch


# set the legend handler for an alluvial diagram
Legend.update_default_handler_map({SubDiagram: AlluvialHandler()})
