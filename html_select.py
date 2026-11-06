"""SoupSieve가 없는 FF에서도 그누보드 기본 선택자를 처리합니다.

추가 설치 없이 태그/ID/클래스/속성, 자손/직계 자식, 쉼표 묶음을 지원합니다.
고급 CSS는 SoupSieve가 설치되어 있을 때 그대로 사용합니다.
"""
import re
from functools import lru_cache

try:
    import soupsieve as _soupsieve
except ImportError:
    _soupsieve = None


class SelectorError(ValueError):
    pass


_TOKEN = re.compile(
    r'''(?P<space>\s+)|(?P<separator>[>,])|'''
    r'''(?P<attribute>\[\s*([\w-]+)\s*(?:([~|^$*]?=)\s*(?:"([^"\\]*)"|'([^'\\]*)'|([\w-]+))\s*)?\])|'''
    r'''(?P<identifier>[.#]?[\w-]+|\*)'''
)
_ATTRIBUTE = re.compile(
    r'''\[\s*([\w-]+)\s*(?:([~|^$*]?=)\s*(?:"([^"\\]*)"|'([^'\\]*)'|([\w-]+))\s*)?\]'''
)


def _invalid():
    return SelectorError('CSS 선택자를 확인하세요. 기본 모드는 태그·#ID·.클래스·[속성]·[속성="값"]·공백·>·쉼표를 지원합니다. 고급 CSS는 soupsieve 설치 후 사용할 수 있습니다.')


@lru_cache(maxsize=128)
def _compile_basic(selector):
    tokens, offset = [], 0
    while offset < len(selector):
        match = _TOKEN.match(selector, offset)
        if match is None:
            raise _invalid()
        tokens.append((match.lastgroup, match.group()))
        offset = match.end()
    groups, chain, relation, index = [], [], None, 0
    while index < len(tokens) and tokens[index][0] == 'space':
        index += 1
    while index < len(tokens):
        compound = []
        while index < len(tokens) and tokens[index][0] in ('identifier', 'attribute'):
            kind, value = tokens[index]
            if kind == 'attribute':
                name, op, double, single, bare = _ATTRIBUTE.fullmatch(value).groups()
                compound.append(('attr', (name.lower(), op, next((x for x in (double, single, bare) if x is not None), None))))
            elif value.startswith('.'):
                compound.append(('class', value[1:]))
            elif value.startswith('#'):
                compound.append(('id', value[1:]))
            else:
                if compound:
                    raise _invalid()
                compound.append(('tag', value.lower()))
            index += 1
        if not compound:
            raise _invalid()
        chain.append((relation, tuple(compound)))
        spaced = False
        while index < len(tokens) and tokens[index][0] == 'space':
            spaced = True
            index += 1
        if index == len(tokens):
            break
        kind, value = tokens[index]
        if kind == 'separator':
            if value == ',':
                groups.append(tuple(chain))
                chain, relation = [], None
            else:
                relation = '>'
            index += 1
            while index < len(tokens) and tokens[index][0] == 'space':
                index += 1
            if index == len(tokens):
                raise _invalid()
        elif spaced:
            relation = ' '
        else:
            raise _invalid()
    if not chain:
        raise _invalid()
    groups.append(tuple(chain))
    return tuple(groups)


def validate_selector(selector):
    if _soupsieve is not None:
        _soupsieve.compile(selector)
    else:
        _compile_basic(selector)


def _attribute_matches(node, condition):
    name, op, expected = condition
    if not node.has_attr(name):
        return False
    if op is None:
        return True
    value = node.get(name)
    value = ' '.join(value) if isinstance(value, list) else str(value or '')
    if op == '=':
        return value == expected
    if not expected:
        return False
    if op == '~=':
        return expected in value.split()
    if op == '|=':
        return value == expected or value.startswith(expected + '-')
    if op == '^=':
        return value.startswith(expected)
    if op == '$=':
        return value.endswith(expected)
    return expected in value  # *=


def _matches(node, compound):
    if not getattr(node, 'name', None) or getattr(node, 'attrs', None) is None:
        return False
    for kind, value in compound:
        if kind == 'tag' and value not in ('*', node.name.lower()):
            return False
        if kind == 'id' and node.get('id') != value:
            return False
        if kind == 'class':
            classes = node.get('class', [])
            if value not in (classes.split() if isinstance(classes, str) else classes):
                return False
        if kind == 'attr' and not _attribute_matches(node, value):
            return False
    return True


def _matches_chain(node, chain, index, root):
    relation, compound = chain[index]
    if not _matches(node, compound):
        return False
    if index == 0:
        return True
    if node is root:
        return False
    parent = node.parent
    if relation == '>':
        return _matches_chain(parent, chain, index - 1, root)
    while parent is not None:
        if _matches_chain(parent, chain, index - 1, root):
            return True
        if parent is root:
            break
        parent = parent.parent
    return False


def select(root, selector, limit=0):
    if _soupsieve is not None:
        return _soupsieve.select(selector, root, limit=limit)
    groups = _compile_basic(selector)
    results = []
    for node in root.find_all(True):
        if any(_matches_chain(node, chain, len(chain) - 1, root) for chain in groups):
            results.append(node)
            if limit and len(results) >= limit:
                break
    return results


def select_one(root, selector):
    result = select(root, selector, limit=1)
    return result[0] if result else None
