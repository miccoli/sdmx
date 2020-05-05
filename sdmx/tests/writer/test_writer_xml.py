import pytest
import sdmx
from sdmx.message import DataMessage


def test_codelist(codelist):
    result = sdmx.to_xml(codelist, pretty_print=True)
    print(result.decode())


def test_structuremessage(structuremessage):
    result = sdmx.to_xml(structuremessage, pretty_print=True)
    print(result.decode())


def test_not_implemented():
    msg = DataMessage()

    with pytest.raises(NotImplementedError,
                       match='write DataMessage to XML'):
        sdmx.to_xml(msg)
