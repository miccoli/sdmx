"""SDMX-ML v2.1 reader."""
# Contents of this file are organized in the order:
#
# - Utility methods and global variables.
# - Reference and Reader classes.
# - Parser functions for sdmx.message classes, in the same order as message.py
# - Parser functions for sdmx.model classes, in the same order as model.py
import logging
import re
from collections import ChainMap, defaultdict
from copy import copy
from importlib import import_module
from itertools import chain, count
from sys import maxsize
from typing import (
    Any,
    ClassVar,
    Dict,
    Iterable,
    Iterator,
    Mapping,
    Optional,
    Sequence,
    Tuple,
    Type,
    Union,
    cast,
)

from dateutil.parser import isoparse
from lxml import etree
from lxml.etree import QName

import sdmx.format.xml
import sdmx.urn
from sdmx import message
from sdmx.exceptions import XMLParseError  # noqa: F401
from sdmx.format import Version, list_media_types
from sdmx.model import common
from sdmx.model import v21 as model
from sdmx.reader.base import BaseReader

log = logging.getLogger(__name__)
log.setLevel(logging.DEBUG)

TO_SNAKE_RE = re.compile("([A-Z]+)")


def add_localizations(target: model.InternationalString, values: list) -> None:
    """Add localized strings from *values* to *target*."""
    target.localizations.update({locale: label for locale, label in values})


def matching_class(cls):
    """Filter condition; see :meth:`.get_single` and :meth:`.pop_all`."""
    return lambda item: isinstance(item, type) and issubclass(item, cls)


def setdefault_attrib(target, elem, *names):
    try:
        for name in names:
            try:
                target.setdefault(to_snake(name), elem.attrib[name])
            except KeyError:
                pass
    except AttributeError:
        pass


def to_snake(value):
    """Convert *value* from lowerCamelCase to snake_case."""
    return TO_SNAKE_RE.sub(r"_\1", value).lower()


class NotReference(Exception):
    """Raised when the `elem` passed to :class:`.Reference` is not a reference."""


# Sentinel value for a missing Agency
_NO_AGENCY = model.Agency()


class _NoText:
    pass


# Sentinel value for XML elements with no text; used to distinguish from "" and None
NoText = _NoText()


class Reference:
    """Temporary class for references.

    - `cls`, `id`, `version`, and `agency_id` are always for a MaintainableArtefact.
    - If the reference target is a MaintainableArtefact (`maintainable` is True),
      `target_cls` and `target_id` are identical to `cls` and `id`, respectively.
    - If the target is not maintainable, `target_cls` and `target_id` describe it.

    `cls_hint` is an optional hint for when the object is instantiated, i.e. a more
    specific override for `cls`/`target_cls`.
    """

    def __init__(self, reader, elem, cls_hint=None):
        parent_tag = elem.tag

        info = self.info_from_element(elem)

        # Find the target class
        target_cls = reader.model.get_class(info["class"], info["package"])

        if target_cls is None:
            # Try the parent tag name
            target_cls = reader.format.class_for_tag(parent_tag)

        if cls_hint and (target_cls is None or issubclass(cls_hint, target_cls)):
            # Hinted class is more specific than target_cls, or failed to find a target
            # class above
            target_cls = cls_hint

        if target_cls is None:
            print(f"{info = }")

        self.maintainable = issubclass(target_cls, common.MaintainableArtefact)

        if self.maintainable:
            # MaintainableArtefact is the same as the target
            cls, info["id"] = target_cls, info["target_id"]
        else:
            # Get the class for the parent MaintainableArtefact
            cls = reader.model.parent_class(target_cls)

        # Store
        self.cls = cls
        self.agency = (
            common.Agency(id=info["agency"]) if info.get("agency", None) else _NO_AGENCY
        )
        self.id = info["id"]
        self.version = info.get("version", None)
        self.target_cls = target_cls
        self.target_id = info["target_id"]

    @classmethod
    def info_from_element(cls, elem):
        try:
            # Use the first child
            elem = elem[0]
        except IndexError:
            raise NotReference

        # Extract information from the XML element
        if elem.tag == "Ref":
            # Element attributes give target_id, id, and version
            result = dict(
                target_id=elem.attrib["id"],
                agency=elem.attrib.get("agencyID", None),
                id=elem.attrib.get("maintainableParentID", elem.attrib["id"]),
                version=elem.attrib.get("maintainableParentVersion", None)
                or elem.attrib.get("version", None),
            )

            # Attributes of the element itself, if any
            for k in ("class", "package"):
                result[k] = elem.attrib.get(k, None)
        elif elem.tag == "URN":
            result = sdmx.urn.match(elem.text)
            # If the URN doesn't specify an item ID, it is probably a reference to a
            # MaintainableArtefact, so target_id and id are the same
            result.update(target_id=result["item_id"] or result["id"])
        else:
            raise NotReference

        return result

    def __str__(self):
        # NB for debugging only
        return (  # pragma: no cover
            f"{self.cls.__name__}={self.agency.id}:{self.id}({self.version}) → "
            f"{self.target_cls.__name__}={self.target_id}"
        )


class DispatchingReader(type, BaseReader):
    """Populate the parser, format, and model attributes of :class:`Reader`."""

    def __new__(cls, name, bases, dct):
        x = super().__new__(cls, name, bases, dct)

        # Empty dictionary
        x.parser = {}

        name = {Version["2.1"]: "v21", Version["3.0.0"]: "v30"}[x.xml_version]
        x.format = import_module(f"sdmx.format.xml.{name}")
        x.model = import_module(f"sdmx.model.{name}")

        return x


class Reader(metaclass=DispatchingReader):
    """SDMX-ML 2.1 reader."""

    # SDMX-ML version handled by this reader
    xml_version: ClassVar = Version["2.1"]
    media_types: ClassVar = list_media_types(base="xml", version=xml_version)
    suffixes: ClassVar = [".xml"]

    # Reference to the module defining the format read
    format: ClassVar
    # Reference to the module defining the model read
    model: ClassVar

    # Mapping from (QName, ["start", "end"]) to a function that parses the element/event
    # or else None
    parser: ClassVar

    Reference: ClassVar = Reference

    # One-way counter for use in stacks
    _count = None

    def __init__(self):
        # Initialize counter
        self._count = count()

    # BaseReader methods

    @classmethod
    def detect(cls, content):
        # NB this should not ever be used directly; rather the .reader.xml.Reader method
        return content.startswith(b"<")  # pragma: no cover

    def read_message(
        self,
        source,
        dsd: Optional[common.BaseDataStructureDefinition] = None,
        _events=None,
    ) -> message.Message:
        # Initialize stacks
        self.stack: Dict[Union[Type, str], Dict[Union[str, int], Any]] = defaultdict(
            dict
        )

        # Elements to ignore when parsing finishes
        self.ignore = set()

        # If calling code provided a DSD, add it to a stack, and let it be ignored when
        # parsing finishes
        self.push(dsd)
        self.ignore.add(id(dsd))

        if _events is None:
            events = cast(
                Iterator[Tuple[str, etree._Element]],
                etree.iterparse(source, events=("start", "end")),
            )
        else:
            events = _events

        try:
            # Use the etree event-driven parser
            # NB (typing) iterparse() returns tuples. For "start" and "end", the second
            #    item is etree._Element, but for other events, e.g. "start-ns", it is
            #    not. types-lxml accurately reflects this. Narrow the type here for the
            #    following code.
            for event, element in events:
                try:
                    # Retrieve the parsing function for this element & event
                    func = self.parser[element.tag, event]
                except KeyError:  # pragma: no cover
                    # Don't know what to do for this (element, event)
                    raise NotImplementedError(element.tag, event) from None

                try:
                    # Parse the element
                    result = func(self, element)
                except TypeError:
                    if func is None:  # Explicitly no parser for this (element, event)
                        continue  # Skip
                    else:  # pragma: no cover
                        raise
                else:
                    # Store the result
                    self.push(result)

                    if event == "end":
                        element.clear()  # Free memory

        except Exception as exc:
            # Parsing failed; display some diagnostic information
            self._dump()
            print(etree.tostring(element, pretty_print=True).decode())
            raise XMLParseError from exc

        # Parsing complete; count uncollected items from the stacks, which represent
        # parsing errors

        # Remove some internal items
        self.pop_single("SS without DSD")
        self.pop_single("DataSetClass")

        # Count only non-ignored items
        uncollected = -1
        for key, objects in self.stack.items():
            uncollected += sum(
                [1 if id(o) not in self.ignore else 0 for o in objects.values()]
            )

        if uncollected > 0:  # pragma: no cover
            self._dump()
            raise RuntimeError(f"{uncollected} uncollected items")

        return cast(message.Message, self.get_single(message.Message, subclass=True))

    @classmethod
    def start(cls, names: str, only: bool = True):
        """Decorator for a function that parses "start" events for XML elements."""

        def decorator(func):
            for tag in map(cls.format.qname, names.split()):
                cls.parser[tag, "start"] = func
                if only:
                    cls.parser[tag, "end"] = None
            return func

        return decorator

    @classmethod
    def end(cls, names: str, only: bool = True):
        """Decorator for a function that parses "end" events for XML elements."""

        def decorator(func):
            for tag in map(cls.format.qname, names.split()):
                cls.parser[tag, "end"] = func
                if only:
                    cls.parser[tag, "start"] = None
            return func

        return decorator

    # Stack handling

    def _clean(self):  # pragma: no cover
        """Remove empty stacks."""
        for key in list(self.stack.keys()):
            if len(self.stack[key]) == 0:
                self.stack.pop(key)

    def _dump(self):  # pragma: no cover
        """Print the stacks, for debugging."""
        self._clean()
        print("\n\n")
        for key, values in self.stack.items():
            print(f"--- {key} ---")
            if isinstance(values, Mapping):
                print(
                    *map(lambda kv: f"{kv[0]} ({id(kv[1])}) {kv[1]!s}", values.items()),
                    sep="\n",
                    end="\n\n",
                )
        print("\nIgnore:\n", self.ignore)

    def push(self, stack_or_obj, obj=None):
        """Push an object onto a stack."""
        if stack_or_obj is None:
            return
        elif obj is None:
            # Add the object to a stack based on its class
            obj = stack_or_obj
            s = stack_or_obj.__class__
        elif isinstance(stack_or_obj, str):
            # Stack with a string name
            s = stack_or_obj
        else:
            # Element; use its local name
            s = QName(stack_or_obj).localname

        # Get the ID for the element in the stack: its .id attribute, if any, else a
        # unique number
        id = getattr(obj, "id", next(self._count)) or next(self._count)

        if id in self.stack[s]:
            # Avoid a collision for two distinct objects with the same ID, e.g. with
            # different maintainers (ECB:AGENCIES vs. SDMX:AGENCIES). Re-insert with
            # numerical keys. This means the objects cannot be retrieved by their ID,
            # but the code does not rely on this.
            self.stack[s][next(self._count)] = self.stack[s].pop(id)
            id = next(self._count)

        self.stack[s][id] = obj

    def stash(self, *stacks):
        """Temporarily hide all objects in the given `stacks`."""
        self.push("_stash", {s: self.stack.pop(s, dict()) for s in stacks})

    def unstash(self):
        """Restore the objects hidden by the last :meth:`stash` call to their stacks.

        Calls to :meth:`.stash` and :meth:`.unstash` should be matched 1-to-1; if the
        latter outnumber the former, this will raise :class:`.KeyError`.
        """
        for s, values in (self.pop_single("_stash") or {}).items():
            self.stack[s].update(values)

    # Delegate to version-specific module
    @classmethod
    def NS(cls):
        return cls.format.NS

    @classmethod
    def class_for_tag(cls, tag: str) -> type:
        return cls.format.class_for_tag(tag)

    @classmethod
    def qname(cls, ns_or_name, name=None) -> QName:
        return cls.format.qname(ns_or_name, name)

    def get_single(
        self,
        cls_or_name: Union[Type, str],
        id: Optional[str] = None,
        version: Optional[str] = None,
        subclass: bool = False,
    ) -> Optional[Any]:
        """Return a reference to an object while leaving it in its stack.

        Always returns 1 object. Returns :obj:`None` if no matching object exists, or if
        2 or more objects meet the conditions.

        If `id` (and `version`) is/are given, only return an IdentifiableArtefact with
        the matching ID (and version).

        If `cls_or_name` is a class and `subclass` is :obj:`True`; check all objects in
        the stack `cls_or_name` *or any stack for a subclass of this class*.
        """
        if subclass:
            keys: Iterable[Union[Type, str]] = filter(
                matching_class(cls_or_name), self.stack.keys()
            )
            results: Mapping = ChainMap(*[self.stack[k] for k in keys])
        else:
            results = self.stack.get(cls_or_name, dict())

        if id and version:
            for v in results.values():
                if v.id == id and v.version == version:
                    return v
            return None
        elif id:
            return results.get(id)
        elif len(results) != 1:
            # 0 or ≥2 results
            return None
        else:
            return next(iter(results.values()))

    def pop_all(self, cls_or_name: Union[Type, str], subclass=False) -> Sequence:
        """Pop all objects from stack *cls_or_name* and return.

        If `cls_or_name` is a class and `subclass` is :obj:`True`; return all objects in
        the stack `cls_or_name` *or any stack for a subclass of this class*.
        """
        if subclass:
            keys: Iterable[Union[Type, str]] = list(
                filter(matching_class(cls_or_name), self.stack.keys())
            )
            result: Iterable = chain(*[self.stack.pop(k).values() for k in keys])
        else:
            result = self.stack.pop(cls_or_name, dict()).values()

        return list(result)

    def pop_single(self, cls_or_name: Union[Type, str]):
        """Pop a single object from the stack for `cls_or_name` and return."""
        try:
            return self.stack[cls_or_name].popitem()[1]
        except KeyError:
            return None

    def peek(self, cls_or_name: Union[Type, str]):
        """Get the object at the top of stack `cls_or_name` without removing it."""
        try:
            key, value = self.stack[cls_or_name].popitem()
            self.stack[cls_or_name][key] = value
            return value
        except KeyError:  # pragma: no cover
            return None

    def pop_resolved_ref(self, cls_or_name: Union[Type, str]):
        """Pop a reference to `cls_or_name` and resolve it."""
        return self.resolve(self.pop_single(cls_or_name))

    def reference(self, elem, cls_hint=None):
        return self.Reference(self, elem, cls_hint=cls_hint)

    def resolve(self, ref):
        """Resolve the Reference instance `ref`, returning the referred object."""
        if not isinstance(ref, Reference):
            # None, already resolved, or not a Reference
            return ref

        # Try to get the target directly
        target = self.get_single(
            ref.target_cls, ref.target_id, ref.version, subclass=True
        )

        if target:
            return target

        # MaintainableArtefact with is_external_reference=True; either a new object, or
        # reference to an existing object
        target_or_parent = self.maintainable(
            ref.cls, None, id=ref.id, maintainer=ref.agency, version=ref.version
        )

        if ref.maintainable:
            # `target_or_parent` is the target
            return target_or_parent

        # At this point, trying to resolve a reference to a child object of a parent
        # MaintainableArtefact; `target_or_parent` is the parent
        parent = target_or_parent

        if parent.is_external_reference:
            # Create the child
            return parent.setdefault(id=ref.target_id)
        else:
            try:
                # Access the child. Mismatch here will raise KeyError
                return parent[ref.target_id]
            except KeyError:
                if isinstance(parent, model.ItemScheme):
                    return parent.get_hierarchical(ref.target_id)
                raise

    def annotable(self, cls, elem, **kwargs):
        """Create a AnnotableArtefact of `cls` from `elem` and `kwargs`.

        Collects all parsed <com:Annotation>.
        """
        if elem is not None:
            kwargs.setdefault("annotations", [])
            kwargs["annotations"].extend(self.pop_all(model.Annotation))
        return cls(**kwargs)

    def identifiable(self, cls, elem, **kwargs):
        """Create a IdentifiableArtefact of `cls` from `elem` and `kwargs`."""
        setdefault_attrib(kwargs, elem, "id", "urn", "uri")
        return self.annotable(cls, elem, **kwargs)

    def nameable(self, cls, elem, **kwargs):
        """Create a NameableArtefact of `cls` from `elem` and `kwargs`.

        Collects all parsed :class:`.InternationalString` localizations of <com:Name>
        and <com:Description>.
        """
        obj = self.identifiable(cls, elem, **kwargs)
        if elem is not None:
            add_localizations(obj.name, self.pop_all("Name"))
            add_localizations(obj.description, self.pop_all("Description"))
        return obj

    def maintainable(self, cls, elem, **kwargs):
        """Create or retrieve a MaintainableArtefact of `cls` from `elem` and `kwargs`.

        Following the SDMX-IM class hierarchy, :meth:`maintainable` calls
        :meth:`nameable`, which in turn calls :meth:`identifiable`, etc. (Since no
        concrete class is versionable but not maintainable, no separate method is
        created, for better performance). For all of these methods:

        - Already-parsed items are removed from the stack only if `elem` is not
          :obj:`None`.
        - `kwargs` (e.g. 'id') take precedence over any values retrieved from
          attributes of `elem`.

        If `elem` is None, :meth:`maintainable` returns a MaintainableArtefact with
        the is_external_reference attribute set to :obj:`True`. Subsequent calls with
        the same object ID will return references to the same object.
        """
        kwargs.setdefault("is_external_reference", elem is None)
        setdefault_attrib(
            kwargs,
            elem,
            "isExternalReference",
            "isFinal",
            "validFrom",
            "validTo",
            "version",
        )
        kwargs["is_final"] = kwargs.get("is_final", None) == "true"

        # Create a candidate object
        obj = self.nameable(cls, elem, **kwargs)

        try:
            # Retrieve the Agency.id for obj.maintainer
            maint = self.get_single(model.Agency, elem.attrib["agencyID"])
        except (AttributeError, KeyError):
            pass
        else:
            # Elem contains a maintainer ID
            if maint is None:
                # …but it did not correspond to an existing object; create one
                maint = model.Agency(id=elem.attrib["agencyID"])
                self.push(maint)
                # This object is never collected; ignore it at end of parsing
                self.ignore.add(id(maint))
            obj.maintainer = maint

        # Maybe retrieve an existing object of the same class and ID
        existing = self.get_single(cls, obj.id)

        if existing and (
            existing.compare(obj, strict=True) or existing.urn == sdmx.urn.make(obj)
        ):
            if elem is not None:
                # Previously an external reference, now concrete
                existing.is_external_reference = False

                # Update `existing` from `obj` to preserve references
                # If `existing` was a forward reference <Ref/>, its URN was not stored.
                for attr in list(kwargs.keys()) + ["urn"]:
                    # log.info(
                    #     f"Updating {attr} {getattr(existing, attr)} "
                    #     f"{getattr(obj, attr)}"
                    # )
                    setattr(existing, attr, getattr(obj, attr))

            # Discard the candidate
            obj = existing
        elif obj.is_external_reference:
            # A new external reference. Ensure it has a URN.
            obj.urn = obj.urn or sdmx.urn.make(obj)
            # Push onto the stack to be located by next calls
            self.push(obj)

        return obj


# Shorthand
start = Reader.start
end = Reader.end

# Tags to skip entirely
start(
    "com:Annotations com:Footer footer:Message "
    # Key and observation values
    "gen:ObsDimension gen:ObsValue gen:Value "
    # Tags that are bare containers for other XML elements
    """
    str:Categorisations str:CategorySchemes str:Codelists str:Concepts
    str:ConstraintAttachment str:Constraints str:CustomTypes str:Dataflows
    str:DataStructureComponents str:DataStructures str:FromVtlSuperSpace
    str:HierarchicalCodelists str:Metadataflows str:MetadataStructures
    str:MetadataStructureComponents str:NamePersonalisations
    str:None str:OrganisationSchemes str:ProvisionAgreements str:Rulesets
    str:StructureSets str:ToVtlSubSpace str:Transformations str:UserDefinedOperators
    str:VtlMappings
    """
    # Contents of references
    ":Ref :URN"
)(None)


# Parsers for sdmx.message classes


@start(
    "mes:Error mes:GenericData mes:GenericTimeSeriesData mes:StructureSpecificData "
    "mes:StructureSpecificTimeSeriesData"
)
@start("mes:Structure", only=False)
def _message(reader: Reader, elem):
    """Start of a Message."""
    # <mes:Structure> within <mes:Header> of a data message is handled by
    # _header_structure() below.
    if getattr(elem.getparent(), "tag", None) == reader.qname("mes", "Header"):
        return

    ss_without_dsd = False

    # With 'dsd' argument, the message should be structure-specific
    if (
        "StructureSpecific" in elem.tag
        and reader.get_single(common.BaseDataStructureDefinition) is None
    ):
        log.warning(f"xml.Reader got no dsd=… argument for {QName(elem).localname}")
        ss_without_dsd = True
    elif "StructureSpecific" not in elem.tag and reader.get_single(
        common.BaseDataStructureDefinition
    ):
        log.info("Use supplied dsd=… argument for non–structure-specific message")

    # Store values for other methods
    reader.push("SS without DSD", ss_without_dsd)
    if "Data" in elem.tag:
        reader.push("DataSetClass", model.get_class(f"{QName(elem).localname}Set"))

    # Handle namespaces mapped on `elem` but not part of the standard set
    for key, value in filter(
        lambda kv: kv[1] not in set(reader.NS().values()), elem.nsmap.items()
    ):
        # Register the namespace
        reader.NS().update({key: value})
        # Use _ds_start() and _ds_end() to handle <{key}:DataSet> elements
        reader.start(f"{key}:DataSet", only=False)(_ds_start)
        reader.end(f"{key}:DataSet", only=False)(_ds_end)

    # Instantiate the message object
    return reader.class_for_tag(elem.tag)()


@end("mes:Header")
def _header(reader, elem):
    # Attach to the Message
    header = message.Header(
        extracted=reader.pop_single("Extracted") or None,
        id=reader.pop_single("ID") or None,
        prepared=reader.pop_single("Prepared") or None,
        receiver=reader.pop_single("Receiver") or None,
        reporting_begin=reader.pop_single("ReportingBegin") or None,
        reporting_end=reader.pop_single("ReportingEnd") or None,
        sender=reader.pop_single("Sender") or None,
        test=str(reader.pop_single("Test")).lower() == "true",
    )
    add_localizations(header.source, reader.pop_all("Source"))

    reader.get_single(message.Message, subclass=True).header = header

    # TODO add these to the Message class
    # Appearing in data messages from WB_WDI and the footer.xml specimen
    reader.pop_all("DataSetAction")
    reader.pop_all("DataSetID")
    # Appearing in the footer.xml specimen
    reader.pop_all("Timezone")


@end("mes:Receiver mes:Sender")
def _header_org(reader, elem):
    reader.push(
        elem,
        reader.nameable(
            reader.class_for_tag(elem.tag), elem, contact=reader.pop_all(model.Contact)
        ),
    )


@end("mes:Structure", only=False)
def _header_structure(reader, elem):
    """<mes:Structure> within <mes:Header> of a DataMessage."""
    # The root node of a structure message is handled by _message(), above.
    if elem.getparent() is None:
        return

    msg = reader.get_single(message.DataMessage)

    # Retrieve a DSD supplied to the parser, e.g. for a structure specific message
    provided_dsd = reader.get_single(common.BaseDataStructureDefinition, subclass=True)

    # Resolve the <com:Structure> child to a DSD, maybe is_external_reference=True
    header_dsd = reader.pop_resolved_ref("Structure")

    # The header may give either a StructureUsage, or a specific reference to a subclass
    # like BaseDataflow. Resolve the <str:StructureUsage> child, if any, and remove it
    # from the stack.
    header_su = reader.pop_resolved_ref("StructureUsage")
    reader.pop_single(type(header_su))

    # Store a specific reference to a data flow specifically
    if isinstance(header_su, reader.class_for_tag("str:Dataflow")):
        msg.dataflow = header_su

    # DSD to use: the provided one; the one referenced by <com:Structure>; or a
    # candidate constructed using the information contained in `header_su` (if any)
    dsd = provided_dsd or (
        reader.maintainable(
            reader.model.DataStructureDefinition,
            None,
            id=header_su.id,
            maintainer=header_su.maintainer,
            version=header_su.version,  # NB this may not always be the case
        )
        if header_su
        else header_dsd
    )

    if header_dsd and header_su:
        # Ensure the constructed candidate and the one given directly are equivalent
        assert header_dsd == dsd
    elif header_su and not provided_dsd:
        reader.push(dsd)
    elif dsd is None:
        raise RuntimeError

    # Store on the data flow
    msg.dataflow.structure = dsd

    # Store under the structure ID, so it can be looked up by that ID
    reader.push(elem.attrib["structureID"], dsd)

    # Store as an object that won't cause a parsing error if it is left over
    reader.ignore.add(id(dsd))

    try:
        # Information about the 'dimension at observation level'
        dim_at_obs = elem.attrib["dimensionAtObservation"]
    except KeyError:
        pass
    else:
        # Store
        if dim_at_obs == "AllDimensions":
            # Use a singleton object
            dim = model.AllDimensions
        elif provided_dsd:
            # Use existing dimension from the provided DSD
            dim = dsd.dimensions.get(dim_at_obs)
        else:
            # Force creation of the 'dimension at observation' level
            dim = dsd.dimensions.getdefault(
                dim_at_obs,
                cls=(
                    model.TimeDimension
                    if "TimeSeries" in elem.getparent().getparent().tag
                    else model.Dimension
                ),
                # TODO later, reduce this
                order=maxsize,
            )
        msg.observation_dimension = dim


@end("footer:Footer")
def _footer(reader, elem):
    # Get attributes from the child <footer:Message>
    args = dict()
    setdefault_attrib(args, elem[0], "code", "severity")
    if "code" in args:
        args["code"] = int(args["code"])

    reader.get_single(message.Message, subclass=True).footer = message.Footer(
        text=list(map(model.InternationalString, reader.pop_all("Text"))), **args
    )


@end("mes:Structures")
def _structures(reader, elem):
    """End of a structure message."""
    msg = reader.get_single(message.StructureMessage)

    # Populate dictionaries by ID
    for attr, name in msg.iter_collections():
        target = getattr(msg, attr)

        # Store using maintainer, ID, and version
        tmp = {
            (getattr(obj.maintainer, "id", None), obj.id, obj.version): obj
            for obj in reader.pop_all(name, subclass=True)
        }

        # Construct string IDs
        if len(set(k[0:2] for k in tmp.keys())) < len(tmp):
            # Some non-unique (maintainer ID, object ID) pairs; include version
            id_expr = "{0}:{1}({2})"
        elif len(set(k[1] for k in tmp.keys())) < len(tmp):
            # Some non-unique object IDs; include maintainer ID
            id_expr = "{0}:{1}"
        else:
            # Only object ID
            id_expr = "{1}"

        for k, obj in tmp.items():
            target[id_expr.format(*k)] = obj


# Parsers for sdmx.model classes
# §3.2: Base structures


@end(
    """
    com:AnnotationTitle com:AnnotationType com:AnnotationURL com:None com:URN
    com:Value mes:DataSetAction mes:DataSetID mes:Email mes:ID mes:Test mes:Timezone
    str:DataType str:Email str:Expression str:NullValue str:OperatorDefinition
    str:PersonalisedName str:Result str:RulesetDefinition str:Telephone str:URI
    str:VtlDefaultName str:VtlScalarType
    """
)
def _text(reader, elem):
    # If elem.text is None, push a sentinel value
    reader.push(elem, elem.text or NoText)


@end("mes:Extracted mes:Prepared mes:ReportingBegin mes:ReportingEnd")
def _datetime(reader, elem):
    text, n = re.subn(r"(.*\.)(\d{6})\d+(\+.*)", r"\1\2\3", elem.text)
    if n > 0:
        log.debug(f"Truncate sub-microsecond time in <{QName(elem).localname}>")

    reader.push(elem, isoparse(text))


@end(
    "com:AnnotationText com:Name com:Description com:Text mes:Source mes:Department "
    "mes:Role str:Department str:Role"
)
def _localization(reader, elem):
    reader.push(
        elem,
        (elem.attrib.get(reader.qname("xml:lang"), model.DEFAULT_LOCALE), elem.text),
    )


@end(
    """
    com:Structure com:StructureUsage str:AttachmentGroup str:ConceptIdentity
    str:ConceptRole str:DimensionReference str:Parent str:Source str:Structure
    str:StructureUsage str:Target str:Enumeration
    """
)
def _ref(reader: Reader, elem):
    cls_hint = None
    if QName(elem).localname in ("Parent", "Target"):
        # Use the *grand*-parent of the <Ref> or <URN> for a class hint
        cls_hint = reader.class_for_tag(elem.getparent().tag)

    reader.push(QName(elem).localname, reader.reference(elem, cls_hint))


@end("com:Annotation")
def _a(reader, elem):
    url = reader.pop_single("AnnotationURL")
    args = dict(
        title=reader.pop_single("AnnotationTitle"),
        type=reader.pop_single("AnnotationType"),
        url=None if url is NoText else url,
    )

    # Optional 'id' attribute
    setdefault_attrib(args, elem, "id")

    a = model.Annotation(**args)
    add_localizations(a.text, reader.pop_all("AnnotationText"))

    return a


# §3.5: Item Scheme


@start(
    """
    str:Agency str:Code str:Category str:Concept str:CustomType str:DataConsumer
    str:DataProvider
    """,
    only=False,
)
def _item_start(reader, elem):
    # Avoid stealing the name & description of the parent ItemScheme from the stack
    # TODO check this works for annotations

    try:
        if elem[0].tag in ("Ref", "URN"):
            # `elem` is a reference, so it has no name/etc.; don't stash
            return
    except IndexError:
        # No child elements; stash() anyway, but it will be a no-op
        pass

    reader.stash(model.Annotation, "Name", "Description")


@end(
    """
    str:Agency str:Code str:Category str:Concept str:DataConsumer str:DataProvider
    """,
    only=False,
)
def _item_end(reader: Reader, elem):
    try:
        # <str:DataProvider> may be a reference, e.g. in <str:ConstraintAttachment>
        item = reader.reference(elem, cls_hint=reader.class_for_tag(elem.tag))
    except NotReference:
        pass
    else:
        # Restore "Name" and "Description" that may have been stashed by _item_start
        reader.unstash()
        return item

    cls = reader.class_for_tag(elem.tag)
    item = reader.nameable(cls, elem)

    # Hierarchy is stored in two ways

    # (1) XML sub-elements of the parent. These have already been parsed.
    for e in elem:
        if e.tag == elem.tag:
            # Found 1 child XML element with same tag → claim 1 child object
            item.append_child(reader.pop_single(cls))

    # (2) through <str:Parent>
    parent = reader.pop_resolved_ref("Parent")
    if parent:
        parent.append_child(item)

    # Agency only
    try:
        item.contact = reader.pop_all(model.Contact)
    except ValueError:
        # NB this is a ValueError from pydantic, rather than AttributeError from Python
        pass

    reader.unstash()
    return item


@end(
    """
    str:AgencyScheme str:Codelist str:ConceptScheme str:CategoryScheme
    str:CustomTypeScheme str:DataConsumerScheme str:DataProviderScheme
    str:NamePersonalisationScheme str:RulesetScheme str:UserDefinedOperatorScheme
    str:VtlMappingScheme
    """
)
def _itemscheme(reader: Reader, elem):
    try:
        # <str:CustomTypeScheme> may be a reference, e.g. in <str:Transformation>
        return reader.reference(elem, cls_hint=reader.class_for_tag(elem.tag))
    except NotReference:
        pass

    cls: Type[common.ItemScheme] = reader.class_for_tag(elem.tag)

    try:
        args = dict(is_partial=elem.attrib["isPartial"])
    except KeyError:  # e.g. ValueList in .v30
        args = {}

    is_ = reader.maintainable(cls, elem, **args)

    # Iterate over all Item objects *and* their children
    iter_all = chain(*[iter(item) for item in reader.pop_all(cls._Item, subclass=True)])

    # Set of objects already added to `items`
    seen: Dict[Any, Any] = dict()

    # Flatten the list, with each item appearing only once
    for i in filter(lambda i: i not in seen, iter_all):
        try:
            is_.append(seen.setdefault(i, i))
        except ValueError:  # pragma: no cover
            # Existing item, e.g. created by a reference in the same message
            # TODO "no cover" since this doesn't occur in the test suite; check whether
            #      this try/except can be removed.
            pass

    return is_


# §3.6: Structure


@end("str:EnumerationFormat str:TextFormat")
def _facet(reader, elem):
    # Convert attribute names from camelCase to snake_case
    args = {to_snake(key): val for key, val in elem.items()}

    # FacetValueType is given by the "textType" attribute. Convert case of the value:
    # in XML, first letter is uppercase; in the spec and Python enum, lowercase. SDMX-ML
    # default is "String".
    tt = args.pop("text_type", "String")
    fvt = model.FacetValueType[f"{tt[0].lower()}{tt[1:]}"]

    # NB Erratum: "isMultiLingual" appears in XSD schemas ("The isMultiLingual attribute
    #    indicates for a text format of type 'string', whether the value should allow
    #    for multiple values in different languages") and in samples, but is not
    #    mentioned anywhere in the information model. Discard.
    args.pop("is_multi_lingual", None)

    # All other attributes are for FacetType
    ft = model.FacetType(**args)

    reader.push(elem, model.Facet(type=ft, value_type=fvt))


@end("str:CoreRepresentation str:LocalRepresentation")
def _rep(reader, elem):
    return common.Representation(
        enumerated=reader.pop_resolved_ref("Enumeration"),
        non_enumerated=list(
            chain(reader.pop_all("EnumerationFormat"), reader.pop_all("TextFormat"))
        ),
    )


# §4.4: Concept Scheme


@end("str:Concept", only=False)
def _concept(reader, elem):
    concept = _item_end(reader, elem)
    concept.core_representation = reader.pop_single(common.Representation)
    return concept


# §3.3: Basic Inheritance


@end(
    "str:Attribute str:Dimension str:GroupDimension str:MeasureDimension "
    "str:PrimaryMeasure str:TimeDimension"
)
def _component(reader: Reader, elem):
    try:
        # May be a reference
        return reader.reference(elem)
    except NotReference:
        pass

    # Object class: {,Measure,Time}Dimension or DataAttribute
    cls = reader.class_for_tag(elem.tag)

    args = dict(
        id=elem.attrib.get("id", common.MissingID),
        concept_identity=reader.pop_resolved_ref("ConceptIdentity"),
        local_representation=reader.pop_single(common.Representation),
    )
    try:
        args["order"] = int(elem.attrib["position"])
    except KeyError:
        pass
    cr = reader.pop_resolved_ref("ConceptRole")
    if cr:
        args["concept_role"] = cr

    # DataAttribute only
    ar = reader.pop_all(model.AttributeRelationship, subclass=True)
    if len(ar):
        assert len(ar) == 1, ar
        args["related_to"] = ar[0]

    # SDMX 2.1 spec §3A, part III, p.140: “The id attribute holds an explicit
    # identification of the component. If this identifier is not supplied, then it is
    # assumed to be the same as the identifier of the concept referenced from the
    # concept identity.”
    if args["id"] is common.MissingID:
        try:
            args["id"] = args["concept_identity"].id
        except AttributeError:
            pass

    return reader.identifiable(cls, elem, **args)


@end("str:AttributeList str:DimensionList str:Group str:MeasureList")
def _cl(reader: Reader, elem):
    try:
        # <str:Group> may be a reference
        return reader.reference(elem, cls_hint=model.GroupDimensionDescriptor)
    except NotReference:
        pass

    # Retrieve the DSD
    dsd = reader.peek("current DSD")
    assert dsd is not None

    # Retrieve the components
    args = dict(components=reader.pop_all(model.Component, subclass=True))

    # Determine the class
    localname = QName(elem).localname
    if localname == "Group":
        cls: Type = model.GroupDimensionDescriptor

        # Replace components with references
        args["components"] = [
            dsd.dimensions.get(ref.target_id)
            for ref in reader.pop_all("DimensionReference")
        ]
    else:
        # SDMX-ML spec for, e.g. DimensionList: "The id attribute is
        # provided in this case for completeness. However, its value is
        # fixed to 'DimensionDescriptor'."
        cls = reader.class_for_tag(elem.tag)
        args["id"] = elem.attrib.get("id", cls.__name__)

    cl = reader.identifiable(cls, elem, **args)

    try:
        # DimensionDescriptor only
        cl.assign_order()
    except AttributeError:
        pass

    # Assign to the DSD eagerly (instead of in _dsd_end()) for reference by next
    # ComponentList e.g. so that AttributeRelationship can reference the
    # DimensionDescriptor
    attr = {
        common.DimensionDescriptor: "dimensions",
        common.AttributeDescriptor: "attributes",
        reader.model.MeasureDescriptor: "measures",
        common.GroupDimensionDescriptor: "group_dimensions",
    }[cl.__class__]
    if attr == "group_dimensions":
        getattr(dsd, attr)[cl.id] = cl
    else:
        setattr(dsd, attr, cl)


# §4.5: Category Scheme


@end("str:Categorisation")
def _cat(reader, elem):
    return reader.maintainable(
        model.Categorisation,
        elem,
        artefact=reader.pop_resolved_ref("Source"),
        category=reader.pop_resolved_ref("Target"),
    )


# §4.6: Organisations


@end("mes:Contact str:Contact")
def _contact(reader, elem):
    contact = model.Contact(
        telephone=reader.pop_single("Telephone"),
        uri=reader.pop_all("URI"),
        email=reader.pop_all("Email"),
    )
    add_localizations(contact.name, reader.pop_all("Name"))
    add_localizations(contact.org_unit, reader.pop_all("Department"))
    add_localizations(contact.responsibility, reader.pop_all("Role"))
    return contact


# §10.3: Constraints


@end("str:Key")
def _key0(reader, elem):
    # NB this method handles two different usages of an identical tag
    parent = QName(elem.getparent()).localname

    if parent == "DataKeySet":
        # DataKey within DataKeySet
        return model.DataKey(
            included=elem.attrib.get("isIncluded", True),
            # Convert MemberSelection/MemberValue from _ms() to ComponentValue
            key_value={
                ms.values_for: model.ComponentValue(
                    value_for=ms.values_for, value=ms.values.pop().value
                )
                for ms in reader.pop_all(model.MemberSelection)
            },
        )
    else:
        # VTLSpaceKey within VTLMapping
        cls = {
            "FromVtlSuperSpace": model.FromVTLSpaceKey,
            "ToVtlSubSpace": model.ToVTLSpaceKey,
        }[parent]

        return cls(key=elem.text)


@end("str:DataKeySet")
def _dks(reader, elem):
    return model.DataKeySet(
        included=elem.attrib["isIncluded"], keys=reader.pop_all(model.DataKey)
    )


@end("com:StartPeriod com:EndPeriod")
def _p(reader, elem):
    # Store by element tag name
    reader.push(
        elem,
        model.Period(
            is_inclusive=elem.attrib["isInclusive"], period=isoparse(elem.text)
        ),
    )


@end("com:TimeRange")
def _tr(reader, elem):
    return model.RangePeriod(
        start=reader.pop_single("StartPeriod"), end=reader.pop_single("EndPeriod")
    )


def _ms_component(reader, elem, kind):
    """Identify the Component for a ValueSelection."""
    try:
        # Navigate from the current ContentConstraint to a ConstrainableArtefact
        cc_content = reader.stack[reader.Reference]
        assert len(cc_content) == 1, (cc_content, reader.stack, elem.attrib)
        obj = reader.resolve(next(iter(cc_content.values())))

        if isinstance(obj, model.DataflowDefinition):
            # The constrained DFD has a corresponding DSD, which has a Dimension- or
            # AttributeDescriptor
            cl = getattr(obj.structure, kind[0])
        elif isinstance(obj, model.DataStructureDefinition):
            # The DSD is constrained directly
            cl = getattr(obj, kind[0])
        else:
            log.warning(f"Not implemented: constraints attached to {type(obj)}")
            cl = None

        # Get the Component
        return cl, cl.get(elem.attrib["id"])
    except AttributeError:
        # Failed because the ContentConstraint is attached to something, e.g.
        # DataProvider, that does not provide an association to a DSD. Try to get a
        # Component from the current scope with matching ID.
        return None, reader.get_single(kind[1], id=elem.attrib["id"], subclass=True)


def _ms_agency_id(elem):
    """Return the MemberSelection → CubeRegion → ContentConstraint → agencyID."""
    try:
        return elem.getparent().getparent().attrib["agencyID"]
    except Exception:  # pragma: no cover
        return None


@end("com:Attribute com:KeyValue")
def _ms(reader, elem):
    """MemberSelection."""
    arg = dict()

    # Identify the component
    # Values are for either a Dimension or Attribute, based on tag name
    kinds = {
        "KeyValue": ("dimensions", model.Dimension),
        "Attribute": ("attributes", model.DataAttribute),
    }
    kind = kinds.get(QName(elem).localname)

    try:
        cl, values_for = _ms_component(reader, elem, kind)
    except KeyError:
        # Maybe work around khaeru/sdmx#102
        # TODO handle quirks via callbacks in data source modules .source.imf
        if _ms_agency_id(elem) == "IMF" and kind[0] == "dimensions":
            log.warning(
                "Work around incorrect use of CubeRegion/KeyValue in IMF "
                "StructureMessage; see https://github.com/khaeru/sdmx/issues/102"
            )
            cl, values_for = _ms_component(reader, elem, kinds["Attribute"])
        else:  # pragma: no cover
            raise

    arg.update(values_for=values_for)

    # Convert to SelectionValue
    mvs = reader.pop_all("Value")
    trv = reader.pop_all(model.TimeRangeValue)
    if mvs:
        arg["values"] = list(map(lambda v: model.MemberValue(value=v), mvs))
    elif trv:
        arg["values"] = trv
    else:  # pragma: no cover
        raise RuntimeError

    if values_for is None:
        log.warning(
            f"{cl} has no {kind[1].__name__} with ID {elem.attrib['id']}; XML element "
            "ignored and SelectionValues discarded"
        )
        return None
    else:
        return model.MemberSelection(**arg)


@end("str:CubeRegion")
def _cr(reader, elem):
    return model.CubeRegion(
        included=elem.attrib["include"],
        # Combine member selections for Dimensions and Attributes
        member={ms.values_for: ms for ms in reader.pop_all(model.MemberSelection)},
    )


@end("str:ContentConstraint")
def _cc(reader, elem):
    cls = reader.class_for_tag(elem.tag)

    # The attribute is called "type" in SDMX-ML 2.1; "role" in 3.0
    for name in "type", "role":
        try:
            cr_str = elem.attrib[name].lower().replace("allowed", "allowable")
        except KeyError:
            pass

    content = set()
    for ref in reader.pop_all(reader.Reference):
        resolved = reader.resolve(ref)
        if resolved is None:
            log.warning(f"Unable to resolve {cls.__name__}.content ref:\n  {ref}")
        else:
            content.add(resolved)

    return reader.maintainable(
        cls,
        elem,
        role=model.ConstraintRole(role=model.ConstraintRoleType[cr_str]),
        content=content,
        data_content_keys=reader.pop_single(model.DataKeySet),
        data_content_region=reader.pop_all(model.CubeRegion),
    )


# §5.2: Data Structure Definition


@end("str:None")
def _ar_kind(reader: Reader, elem):
    return reader.class_for_tag(elem.tag)()


@end("str:AttributeRelationship")
def _ar(reader, elem):
    dsd = reader.peek("current DSD")

    refs = reader.pop_all(reader.Reference)
    if not len(refs):
        return

    # Iterate over parsed references to Components
    args = dict(dimensions=list())
    for ref in refs:
        # Use the <Ref id="..."> to retrieve a Component from the DSD
        if issubclass(ref.target_cls, model.DimensionComponent):
            component = dsd.dimensions.get(ref.target_id)
            args["dimensions"].append(component)
        elif ref.target_cls is model.PrimaryMeasure:
            # Since <str:AttributeList> occurs before <str:MeasureList>, this is
            # usually a forward reference. We *could* eventually resolve it to confirm
            # consistency (the referenced ID is same as the PrimaryMeasure.id), but
            # that doesn't affect the returned value, since PrimaryMeasureRelationship
            # has no attributes.
            return model.PrimaryMeasureRelationship()
        elif ref.target_cls is model.GroupDimensionDescriptor:
            args["group_key"] = dsd.group_dimensions[ref.target_id]

    ref = reader.pop_single("AttachmentGroup")
    if ref:
        args["group_key"] = dsd.group_dimensions[ref.target_id]

    if len(args["dimensions"]):
        return common.DimensionRelationship(**args)
    else:
        args.pop("dimensions")
        return common.GroupRelationship(**args)


@start("str:DataStructure", only=False)
def _dsd_start(reader: Reader, elem):
    try:
        # <str:DataStructure> may be a reference, e.g. in <str:ConstraintAttachment>
        return reader.reference(elem)
    except NotReference:
        pass

    # Get any external reference created earlier, or instantiate a new object.
    dsd = reader.maintainable(reader.model.DataStructureDefinition, elem)

    if dsd not in reader.stack[reader.model.DataStructureDefinition]:
        # A new object was created
        reader.push(dsd)

    # Store a separate reference to the current DSD
    reader.push("current DSD", dsd)


@end("str:DataStructure", only=False)
def _dsd_end(reader, elem):
    dsd = reader.pop_single("current DSD")

    if dsd:
        # Collect annotations, name, and description
        dsd.annotations = list(reader.pop_all(model.Annotation))
        add_localizations(dsd.name, reader.pop_all("Name"))
        add_localizations(dsd.description, reader.pop_all("Description"))


@end("str:Dataflow str:Metadataflow")
def _dfd(reader: Reader, elem):
    try:
        # <str:Dataflow> may be a reference, e.g. in <str:ConstraintAttachment>
        return reader.reference(elem)
    except NotReference:
        pass

    structure = reader.pop_resolved_ref("Structure")
    if structure is None:
        log.warning(
            "Not implemented: forward reference to:\n" + etree.tostring(elem).decode()
        )
        arg = {}
    else:
        arg = dict(structure=structure)

    # Create first to collect names
    return reader.maintainable(reader.class_for_tag(elem.tag), elem, **arg)


# §5.4: Data Set


@end("gen:Attributes")
def _avs(reader, elem):
    ad = reader.get_single("DataSet").structured_by.attributes

    result = {}
    for e in elem.iterchildren():
        da = ad.getdefault(e.attrib["id"])
        result[da.id] = model.AttributeValue(value=e.attrib["value"], value_for=da)

    reader.push("Attributes", result)


@end("gen:ObsKey gen:GroupKey gen:SeriesKey")
def _key1(reader, elem):
    cls = reader.class_for_tag(elem.tag)

    kv = {e.attrib["id"]: e.attrib["value"] for e in elem.iterchildren()}

    dsd = reader.get_single("DataSet").structured_by

    return dsd.make_key(cls, kv, extend=True)


@end("gen:Series")
def _series(reader, elem):
    ds = reader.get_single("DataSet")
    sk = reader.pop_single(model.SeriesKey)
    sk.attrib.update(reader.pop_single("Attributes") or {})
    ds.add_obs(reader.pop_all(model.Observation), sk)


@end(":Series")
def _series_ss(reader, elem):
    ds = reader.get_single("DataSet")
    ds.add_obs(
        reader.pop_all(model.Observation),
        ds.structured_by.make_key(
            model.SeriesKey, elem.attrib, extend=reader.peek("SS without DSD")
        ),
    )


@end("gen:Group")
def _group(reader, elem):
    ds = reader.get_single("DataSet")

    gk = reader.pop_single(model.GroupKey)
    gk.attrib.update(reader.pop_single("Attributes") or {})

    # Group association of Observations is done in _ds_end()
    ds.group[gk] = []


@end(":Group")
def _group_ss(reader, elem):
    ds = reader.get_single("DataSet")
    attrib = copy(elem.attrib)

    group_id = attrib.pop(reader.qname("xsi", "type"), None)

    gk = ds.structured_by.make_key(
        model.GroupKey, attrib, extend=reader.peek("SS without DSD")
    )

    if group_id:
        # The group_id is in a format like "foo:GroupName", where "foo" is an XML
        # namespace
        ns, group_id = group_id.split(":")
        assert ns in elem.nsmap

        try:
            gk.described_by = ds.structured_by.group_dimensions[group_id]
        except KeyError:
            if not reader.peek("SS without DSD"):
                raise

    ds.group[gk] = []


@end("gen:Obs")
def _obs(reader, elem):
    dim_at_obs = reader.get_single(message.DataMessage).observation_dimension
    dsd = reader.get_single("DataSet").structured_by

    args = dict()

    for e in elem.iterchildren():
        localname = QName(e).localname
        if localname == "Attributes":
            args["attached_attribute"] = reader.pop_single("Attributes")
        elif localname == "ObsDimension":
            # Mutually exclusive with ObsKey
            args["dimension"] = dsd.make_key(
                model.Key, {dim_at_obs.id: e.attrib["value"]}
            )
        elif localname == "ObsKey":
            # Mutually exclusive with ObsDimension
            args["dimension"] = reader.pop_single(model.Key)
        elif localname == "ObsValue":
            args["value"] = e.attrib["value"]

    return model.Observation(**args)


@end(":Obs")
def _obs_ss(reader, elem):
    # True if the user failed to provide a DSD to use in parsing structure-specific data
    extend = reader.peek("SS without DSD")

    # Retrieve the PrimaryMeasure from the DSD for the current data set
    dsd = reader.get_single("DataSet").structured_by

    try:
        # Retrieve the PrimaryMeasure in a supplied DSD, or one created in a previous
        # call to _obs_ss()
        pm = dsd.measures[0]
    except IndexError:
        # No measures in the DSD
        if extend:
            # Create one, assuming the ID OBS_VALUE
            # TODO also add an external reference to the SDMX cross-domain concept
            pm = model.PrimaryMeasure(id="OBS_VALUE")
            dsd.measures.append(pm)
        else:  # pragma: no cover
            raise  # DSD was provided but lacks a PrimaryMeasure

    # StructureSpecificData message—all information stored as XML attributes of the
    # <Observation>
    attrib = copy(elem.attrib)

    # Observation value from an attribute; usually "OBS_VALUE"
    value = attrib.pop(pm.id, None)

    # Extend the DSD if the user failed to provide it
    key = dsd.make_key(model.Key, attrib, extend=extend)

    # Remove attributes from the Key to be attached to the Observation
    aa = key.attrib
    key.attrib = {}

    return model.Observation(
        dimension=key, value=value, value_for=pm, attached_attribute=aa
    )


@start("mes:DataSet", only=False)
def _ds_start(reader, elem):
    # Create an instance of a DataSet subclass
    ds = reader.peek("DataSetClass")()

    # Retrieve the (message-local) ID referencing a data structure definition
    id = elem.attrib.get("structureRef", None) or elem.attrib.get(
        reader.qname("data:structureRef"), None
    )

    # Get a reference to the DSD that structures the data set
    # Provided in the <mes:Header> / <mes:Structure>
    dsd = reader.get_single(id)
    if not dsd:
        # Fall back to a DSD provided as an argument to read_message()
        dsd = reader.get_single(reader.model.DataStructureDefinition)

        if not dsd:  # pragma: no cover
            raise RuntimeError("No DSD when creating DataSet")

        log.debug(
            f'Use provided {dsd!r} for structureRef="{id}" not defined in message'
        )

    ds.structured_by = dsd

    reader.push("DataSet", ds)


@end("mes:DataSet", only=False)
def _ds_end(reader, elem):
    ds = reader.pop_single("DataSet")

    # Collect attributes attached to the data set
    ds.attrib.update(reader.pop_single("Attributes") or {})

    # Collect observations not grouped by SeriesKey
    ds.add_obs(reader.pop_all(model.Observation))

    # Add any group associations not made above in add_obs() or in _series()
    for obs in ds.obs:
        ds._add_group_refs(obs)

    # Add the data set to the message
    reader.get_single(message.DataMessage).data.append(ds)


# §7.3: Metadata Structure Definition


@end("str:MetadataTarget")
def _mdt(reader: Reader, elem):  # pragma: no cover
    raise NotImplementedError


@end("str:MetadataStructure")
def _msd(reader: Reader, elem):  # pragma: no cover
    cls = reader.class_for_tag(elem)
    log.warning(f"Not parsed: {elem.tag} -> {cls}")
    return NotImplemented


# §11: Data Provisioning


@end("str:ProvisionAgreement")
def _pa(reader, elem):
    return reader.maintainable(
        model.ProvisionAgreement,
        elem,
        structure_usage=reader.pop_resolved_ref("StructureUsage"),
        data_provider=reader.pop_resolved_ref(Reference),
    )


# §??: Validation and Transformation Language


@end("str:CustomType", only=False)
def _ct(reader: Reader, elem):
    ct = _item_end(reader, elem)
    ct.data_type = reader.pop_single("DataType")
    ct.null_value = reader.pop_single("NullValue")
    ct.vtl_scalar_type = reader.pop_single("VtlScalarType")
    return ct


@end("str:NamePersonalisation")
def _np(reader: Reader, elem):
    np = _item_end(reader, elem)
    np.personalised_name = reader.pop_single("PersonalisedName")
    np.vtl_default_name = reader.pop_single("VtlDefaultName")
    return np


@end("str:FromVtlMapping")
def _vtlm_from(reader: Reader, elem):
    return common.VTLtoSDMX[elem.attrib.get("method", "basic").lower()]


@end("str:ToVtlMapping")
def _vtlm_to(reader: Reader, elem):
    return common.SDMXtoVTL[elem.attrib.get("method", "basic").lower()]


# @start("str:Key")
# def _vtl_sk(reader: Reader, elem):


@end("str:Ruleset")
def _rs(reader: Reader, elem):
    # TODO handle .scope, .type
    return reader.nameable(
        model.Ruleset, elem, definition=reader.pop_single("RulesetDefinition")
    )


@end("str:Transformation")
def _trans(reader: Reader, elem):
    # TODO handle .is_persistent
    return reader.nameable(
        model.Transformation,
        elem,
        expression=reader.pop_single("Expression"),
        result=reader.pop_single("Result"),
    )


@end("str:TransformationScheme")
def _ts(reader: Reader, elem):
    ts = _itemscheme(reader, elem)

    while True:
        ref = reader.pop_single(reader.Reference)
        try:
            resolved = reader.resolve(ref)
            ts.update_ref(resolved)
        except TypeError:
            reader.push(ref)
            break

    return ts


@end("str:UserDefinedOperator")
def _udo(reader: Reader, elem):
    return reader.nameable(
        model.UserDefinedOperator,
        elem,
        definition=reader.pop_single("OperatorDefinition"),
    )


@end("str:VtlMapping")
def _vtlm(reader: Reader, elem):
    ref = reader.resolve(reader.pop_single(reader.Reference))
    args: Dict[str, Any] = dict()
    if isinstance(ref, common.BaseDataflow):
        cls = model.VTLDataflowMapping
        args["dataflow_alias"] = ref
        args["to_vtl_method"] = reader.pop_single(common.SDMXtoVTL)
        args["to_vtl_subspace"] = reader.pop_all(common.ToVTLSpaceKey)
        args["from_vtl_method"] = reader.pop_single(common.VTLtoSDMX)
        args["from_vtl_superspace"] = reader.pop_all(common.FromVTLSpaceKey)
    else:
        cls = model.VTLConceptMapping
        args["concept_alias"] = ref

    return reader.nameable(cls, elem, **args)
