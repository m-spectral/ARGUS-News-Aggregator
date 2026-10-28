def obj(properties):
    return {
        'type': 'object',
        'properties': properties,
        'required': list(properties),
        'additionalProperties': False,
    }

def array(items):
    return {'type': 'array', 'items': items}

def text(*values):
    result = {'type': 'string'}
    if values:
        result['enum'] = list(values)
    return result

def nullable(schema):
    return {'anyOf': [schema, {'type': 'null'}]}

def integer(maximum):
    return {'type': 'integer', 'minimum': 0, 'maximum': maximum}

def item_schema():
    parts = {'fit': integer(4)}
    parts.update({'usability': integer(3), 'rigor': integer(2)})
    parts['novelty'] = integer(1)
    tags = ('evidence', 'case', 'policy', 'adoption', 'field', 'tool', 'angle')
    result = {
        'id': integer(2 ** 53 - 1),
        'headline': text(),
        'in_scope': {'type': 'boolean'},
        'tags': array(text(*tags)),
        'primary': text(*tags),
        'source_grade': text('A', 'B', 'C', 'D'),
        'source_basis': text('primary_research', 'primary_document', 'documented_reporting', 'named_reporting', 'commentary', 'promotion', 'rumor', 'unknown'),
        'source_note': text(),
        'score_parts': obj(parts),
        'novelty_basis': nullable(text()),
        'evidence': array(text()),
        'implication': text(),
        'recommendation': nullable(obj({
            'action': text(),
            'owner': text(),
            'date': text(),
            'date_type': text('reported', 'proposed'),
        })),
        'reported_dates': array(obj({'date': text(), 'event': text(), 'quote': text()})),
        'figures': array(obj({
            'value': text(),
            'unit': nullable(text()),
            'base': nullable(text()),
            'date': nullable(text()),
            'quote': text(),
        })),
        'research': nullable(obj({
            'design': nullable(text()),
            'sample': nullable(text()),
            'population': nullable(text()),
            'measure': nullable(text()),
            'venue': nullable(text()),
            'review_status': text('peer_reviewed', 'preprint', 'unknown'),
            'replication': nullable(text()),
            'main_limit': nullable(text()),
            'design_supports_claim': nullable({'type': 'boolean'}),
        })),
        'gaps': array(text()),
    }
    return obj({'items': array(obj(result))})

def brief_schema(allowed_ids=None):
    reference = integer(2 ** 53 - 1)
    if allowed_ids is not None:
        reference['enum'] = sorted(set(allowed_ids))
    refs = array(reference)
    def entry(detail):
        return array(obj({'headline': text(), 'summary': text(), detail: text(), 'item_ids': refs}))
    return obj({'brief': obj({
        'market_moving': entry('market_impact'),
        'business': entry('operational_takeaway'),
        'missteps': entry('lesson'),
        'research': entry('why_it_matters'),
    })})

def validate(value, schema, path='response'):
    if 'anyOf' in schema:
        for option in schema['anyOf']:
            try:
                validate(value, option, path)
                return
            except ValueError:
                pass
        raise ValueError(f'{path}: invalid nullable value')
    kind = schema['type']
    valid = {
        'object': isinstance(value, dict),
        'array': isinstance(value, list),
        'integer': type(value) is int,
        'string': isinstance(value, str),
        'boolean': type(value) is bool,
        'null': value is None,
    }[kind]
    if not valid:
        raise ValueError(f'{path}: expected {kind}')
    if 'enum' in schema and value not in schema['enum']:
        raise ValueError(f'{path}: invalid option')
    if kind == 'integer' and (not schema.get('minimum', value) <= value <= schema.get('maximum', value)):
        raise ValueError(f'{path}: integer out of range')
    if kind == 'object':
        if set(value) != set(schema['required']):
            raise ValueError(f'{path}: missing or unexpected fields')
        for (key, item) in value.items():
            validate(item, schema['properties'][key], path + '.' + key)
    elif kind == 'array':
        for (index, item) in enumerate(value):
            validate(item, schema['items'], f'{path}[{index}]')
