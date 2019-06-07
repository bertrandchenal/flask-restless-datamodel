from .helpers import register_serializer, run_object_method
from flask import current_app
import inspect
from sqlalchemy.inspection import inspect as sqla_inspect
from sqlalchemy.ext.hybrid import hybrid_property
from flask_restless.helpers import (primary_key_name,
                                    get_related_association_proxy_model)
from sqlalchemy.ext.associationproxy import AssociationProxy
from sqlalchemy.orm.properties import RelationshipProperty, ColumnProperty
from copy import copy


def get_is_valid_validator(included, excluded):
    def is_valid(column):
        column = column.split('.')[-1]
        valid_excl = True
        valid_incl = True
        if excluded:
            valid_excl = column not in excluded
        if included:
            valid_incl = column in included
        return valid_excl and valid_incl
    return is_valid


class DataModelRenderer:
    def __init__(self, app, db):
        self.app = app
        self.db = db

    def render(self, models, method_renderer):
        data_model = {}
        flagged_for_inheritence = {}
        model_renderer = ClassDefinitionRenderer(self.app)

        for model, kwargs in models.items():
            name = model.__name__
            model_render = model_renderer.render(model, **kwargs)
            model_render['methods'] = method_renderer.methods[name]
            polymorphic_info = self.flag_inheritance_models(model)
            if polymorphic_info:
                flagged_for_inheritence[name] = polymorphic_info
            data_model[name] = model_render

        # update child models with the attributes and relations of their parent
        for model, inheriting_from in flagged_for_inheritence.items():
            self.resolve_inheritance(model, inheriting_from, data_model)
        return data_model

    def flag_inheritance_models(self, model):
        is_polymorphic = (hasattr(model, '__mapper_args__') and
                          'polymorphic_identity' in model.__mapper_args__)
        if is_polymorphic:
            
            parent = None
            for kls in model.__bases__:
                if issubclass(kls, self.db.Model) and kls is not self.db.Model:
                    parent = kls
            if parent:
                on = parent.__mapper_args__['polymorphic_on']
                if not isinstance(on, str):
                    on = on.key
                return {
                    'on': on,
                    'parent': parent.__name__,
                    'identity': model.__mapper_args__['polymorphic_identity'],
                }

    def resolve_inheritance(self, model, inheriting_from, data_model):
        idef = data_model[inheriting_from['parent']]
        odef = data_model[model]
        odef['attributes'].update(idef['attributes'])
        odef['relations'].update(idef['relations'])
        odef['polymorphic'] = {'parent': inheriting_from['parent']}
        idef.setdefault('polymorphic', {})['on'] = inheriting_from['on']
        idef['polymorphic'].setdefault(
            'identities', {})[inheriting_from['identity']] = model


class ClassDefinitionRenderer:
    def __init__(self, app):
        self.app = app

    def render(self, model, collection_name, bp_name, included, excluded):
        is_valid = get_is_valid_validator(included, excluded)

        attribute_dict = self.render_attributes(model, is_valid)
        foreign_keys = self.render_relations(model, is_valid)
        attribute_dict.update(self.render_hybrid_properties(model, is_valid))
        self.render_association_proxies(model, attribute_dict, foreign_keys)

        with self.app.app_context():
            pk_name = primary_key_name(model)

        register_serializer(model, pk_name)

        return {
            'pk_name': pk_name,
            'collection_name': collection_name,
            'attributes': attribute_dict,
            'relations': foreign_keys,
        }

    def render_attributes(self, model, is_valid):
        tbl = model.__table__
        attribute_dict = {}
        for column in tbl.columns:
            if is_valid(column.name):
                ctype = column.type.__class__.__name__.lower()
                attribute_dict[column.name] = ctype
        return attribute_dict

    def render_relations(self, model, is_valid):
        foreign_keys = {}
        for rel in sqla_inspect(model).relationships:
            if is_valid(str(rel.key)):
                direction = rel.direction.name
                if rel.direction.name == 'ONETOMANY' and not rel.uselist:
                    direction = 'ONETOONE'
                foreign_keys[rel.key] = {
                    'foreign_model': rel.mapper.class_.__name__,
                    'relation_type': direction,
                    'backref': rel.back_populates,
                }
                if rel.direction.name == 'MANYTOONE':
                    local_id = list(rel.local_columns)[0].key
                    foreign_keys[rel.key]['local_column'] = local_id
        return foreign_keys

    def render_hybrid_properties(self, model, is_valid):
        attribute_dict = {}
        hybrid_properties = [a for a in sqla_inspect(model).all_orm_descriptors
                             if isinstance(a, hybrid_property)]
        for attribute in hybrid_properties:
            if is_valid(attribute):
                attribute_dict[attribute.__name__] = 'hybrid'
        return attribute_dict

    def render_association_proxies(self, model, attribute_dict, foreign_keys):
        proxies = {}
        for k in list(model.__dict__.keys()):
            v = model.__dict__[k]
            is_proxy = isinstance(v, AssociationProxy)
            # keep the proxies where the remote attr has a property,
            # as we need this property to identify the remote class
            # but not all cases have it.
            # v == v.__get__(None, model), but we do this to bind the model to
            # the remote_attr and from then on it's usable for further inspection
            if is_proxy and hasattr(v.__get__(None, model).remote_attr, 'property'):
                proxies[k] = v.__get__(None, model)

        for name, attr in proxies.items():
            # check if the remote attr is a relation (for example, an association
            # table) or if it's an attribute
            if isinstance(attr.remote_attr.property, RelationshipProperty):
                # use the helper function from flask restless to identify the
                # remote class
                remote_class = get_related_association_proxy_model(attr)
                foreign_keys[name] = {
                    'foreign_model': remote_class.__name__,
                    'relation_type': 'MANYTOONE' if attr.scalar else 'ONETOMANY',
                    'is_proxy': True
                }
            elif isinstance(attr.remote_attr.property, ColumnProperty):
                # The columns of remote attr will always be 1 element in size
                # as the columns is refering to itself (i.e. the remote attr)
                column = attr.remote_attr.property.columns[0]
                attribute_dict[name] = column.type.__class__.__name__.lower()


class MethodDefinitionRenderer:
    def __init__(self, app, options):
        self.app = app
        self.options = options
        self.methods = {}

    def render(self, model, collection_name):
        name = model.__name__
        self.methods[name] = self.compile_method_list(model)
        self.add_method_endpoints(collection_name, model, self.methods[name])

    def compile_method_list(self, model):
        methods = {}
        include_internal = self.options.get('include_model_internal_functions', False)
        for name, fn in inspect.getmembers(model, predicate=inspect.isfunction):
            if name.startswith('__'):
                continue
            if name.startswith('_') and not include_internal:
                continue

            spec = inspect.signature(fn)
            required = []
            optional = []
            argsvar = None
            kwargsvar = None
            for param_name, param in spec.parameters.items():
                if param_name == 'self':
                    continue
                if param.kind == param.VAR_KEYWORD:
                    kwargsvar = param_name
                elif param.kind == param.VAR_POSITIONAL:
                    argsvar = param_name
                elif param.default == param.empty:
                    required.append(param_name)
                else:
                    optional.append(param_name)

            methods[name] = {
                'args': required,
                'kwargs': optional,
                'argsvar': argsvar,
                'kwargsvar': kwargsvar,
            }
        return methods

    def add_method_endpoints(self, collection_name, model, methods):
        for method in methods.keys():
            fmt = '/api/method/{0}/<instid>/{1}'
            instance_endpoint = fmt.format(collection_name, method)
            self.app.add_url_rule(
                instance_endpoint,
                methods=['POST'],
                defaults={
                    'function_name': method,
                    'model': model,
                },
                view_func=run_object_method)