# encoding: utf-8


from pandasdmx.utils import DictLike, str_type
from pandasdmx import model
from .common import Reader
from lxml import objectify
from itertools import repeat


 
class SDMXMLReader(Reader):
    
    """
    Read SDMX-ML 2.1 and expose it as instances from pandasdmx.model
    """
    
    namespaces = {
        'str': 'http://www.sdmx.org/resources/sdmxml/schemas/v2_1/structure',
        'mes': 'http://www.sdmx.org/resources/sdmxml/schemas/v2_1/message',
        'com': 'http://www.sdmx.org/resources/sdmxml/schemas/v2_1/common'}


    model_map = {
        '{http://www.sdmx.org/resources/sdmxml/schemas/v2_1/structure}DataStructureDefinition' : model.DataStructureDefinition
                 
                 }
    
    def parse(self, source):
        root = objectify.parse(source).getroot()
        return model.Message(self, root) 
        
    def dispatch(self, elem):
        model_class = self.model_map.get(elem.tag)
        if model_class: return model_class(self, elem)
        else: return elem
         
        
        
    def mes_header(self, elem):
        'return a message header. elem must be the document root.'
        return model.Header(self, elem.xpath('mes:Header', namespaces = elem.nsmap)[0])
    
    def header_id(self, elem):
        return elem.ID[0].text 
        
    def identity(self, elem):
        return elem.get('id')
    
    def urn(self, elem):
        return elem.get('urn')

    def uri(self, elem):
        return elem.get('uri')
        
        
    def agencyID(self, elem):
        return elem.get('agencyID')
    
    
    def _international_string(self, elem, tagname):
        languages = elem.xpath('com:{0}/@xml:lang'.format(tagname), 
                               namespaces = elem.nsmap)
        strings = elem.xpath('com:{0}/text()'.format(tagname), 
                             namespaces = elem.nsmap)
        return DictLike(zip(languages, strings))

    def description(self, elem):
        return self._international_string(elem, 'Description') 
        
    def name(self, elem):
        return self._international_string(elem, 'Name') 
        

    def header_prepared(self, elem):
        return elem.Prepared[0].text # convert this to datetime obj?
        
    def header_sender(self, elem):
        return DictLike(elem.Sender.attrib)

    def header_error(self, elem):
        try:
            return DictLike(elem.Error.attrib)
        except AttributeError: return None
                     
    def get_items(self, elem, tagname = None, target_cls = None, arg = None):
        if arg:
            if isinstance(arg, str_type):
                return target_cls(self, elem.xpath('str:{0}[@ID = $value]'.format(tagname), 
                    value = arg, namespaces = elem.nsmap)) 
            else: 
                return target_cls(self, elem.xpath('str:{0}[$value]'.format(tagname),
                    value = arg, namespaces = elem.nsmap))
        else: return map(target_cls, repeat(self), elem.xpath('str:{0}'.format(tagname), 
                    namespaces = elem.nsmap)) 
                     
    def codelists(self, elem, value = None):
        'return Codelist by ID or index or iterator of all codelists in a message'
        return self.get_items(elem, tagname = 'Codelist', target_cls = model.Codelist, value = value)
      
        
    def iter_codes(self, elem, value = None):
        return self.get_items(elem, tagname = 'Code', target_cls = model.Code, value = value)
    
    def concept_schemes(self, elem, value = None):
        'return scheme by index or ID or iterator of concept schemes in a message'
        return self.get_items(elem, tagname = 'Concepts', target_cls = model.ConceptScheme, value = value) 
        
    def iter_concepts(self, elem, value = None):
        return self.get_items(elem, tagname = 'Concept', target_cls = model.Concept, value = value)

        
    def isfinal(self, elem):
        return bool(elem.get('isFinal')) 
        
        

 
    def parse_series(self, source):
        """
        generator to parse data from xml. Iterate over series
        """
        CodeTuple = None
        generic_ns = '{http://www.sdmx.org/resources/sdmxml/schemas/v2_1/data/generic}'
        series_tag = generic_ns + 'Series'
        serieskey_tag = series_tag + 'Key'
        value_tag = generic_ns + 'Value'
        obs_tag = generic_ns + 'Obs'
        obsdim_tag = generic_ns + 'ObsDimension'
        obsvalue_tag = generic_ns + 'ObsValue'
        attributes_tag = generic_ns + 'Attributes' 
        context = lxml.etree.iterparse(source, tag = series_tag)
        
        for _, series in context: 
            raw_dates, raw_values, raw_status = [], [], []
            
            for elem in series.iterchildren():
                if elem.tag == serieskey_tag:
                    code_keys, code_values = [], []
                    for value in elem.iter(value_tag):
                        if not CodeTuple: code_keys.append(value.get('id')) 
                        code_values.append(value.get('value'))
                elif elem.tag == obs_tag:
                    for elem1 in elem.iterchildren():
                        observation_status = 'A'
                        if elem1.tag == obsdim_tag:
                            dimension = elem1.get('value')
                            # Prepare time spans such as Q1 or S2 to make it parsable
                            suffix = dimension[-2:]
                            if suffix in time_spans:
                                dimension = dimension[:-2] + time_spans[suffix]
                            raw_dates.append(dimension) 
                        elif elem1.tag == obsvalue_tag:
                            value = elem1.get('value')
                            raw_values.append(value)
                        elif elem1.tag == attributes_tag:
                            for elem2 in elem1.iter(".//"+generic_ns+"Value[@id='OBS_STATUS']"):
                                observation_status = elem2.get('value')
                            raw_status.append(observation_status)
            if not CodeTuple:
                CodeTuple = make_namedtuple(code_keys) 
            codes = CodeTuple._make(code_values)
            series.clear()
            yield codes, raw_dates, raw_values, raw_status 
    
    
        