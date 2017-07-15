from __future__ import absolute_import

from collections import OrderedDict
from operator import attrgetter

import cgen as c
import numpy as np
from sympy import Symbol

from devito.cgen_utils import ccode
from devito.dse import as_symbol
from devito.dle import retrieve_iteration_tree, filter_iterations
from devito.dle.backends import AbstractRewriter, dle_pass, complang_ALL
from devito.interfaces import ScalarFunction
from devito.nodes import (Denormals, Expression, FunCall, Function, List,
                          UnboundedIndex)
from devito.tools import filter_sorted, flatten
from devito.visitors import FindNodes, FindSymbols, NestedTransformer, Transformer


class BasicRewriter(AbstractRewriter):

    def _pipeline(self, state):
        self._avoid_denormals(state)
        self._create_elemental_functions(state)

    @dle_pass
    def _avoid_denormals(self, state, **kwargs):
        """
        Introduce nodes in the Iteration/Expression tree that will expand to C
        macros telling the CPU to flush denormal numbers in hardware. Denormals
        are normally flushed when using SSE-based instruction sets, except when
        compiling shared objects.
        """
        return {'nodes': (Denormals(),) + state.nodes,
                'includes': ('xmmintrin.h', 'pmmintrin.h')}

    @dle_pass
    def _create_elemental_functions(self, state, **kwargs):
        """
        Extract :class:`Iteration` sub-trees and move them into :class:`Function`s.

        Currently, only tagged, elementizable Iteration objects are targeted.
        """
        noinline = self._compiler_decoration('noinline', c.Comment('noinline?'))

        functions = OrderedDict()
        processed = []
        for node in state.nodes:
            mapper = {}
            for tree in retrieve_iteration_tree(node, mode='superset'):
                # Search an elementizable sub-tree (if any)
                tagged = filter_iterations(tree, lambda i: i.tag is not None, 'asap')
                if not tagged:
                    continue
                root = tagged[0]
                if not root.is_Elementizable:
                    continue
                target = tree[tree.index(root):]

                # Elemental function arguments
                args = []  # Found so far (scalars, tensors)
                maybe_required = set()  # Scalars that *may* have to be passed in
                not_required = set()  # Elemental function locally declared scalars

                # Build a new Iteration/Expression tree with free bounds
                free = []
                for i in target:
                    name, bounds = i.dim.name, i.bounds_symbolic
                    # Iteration bounds
                    start = ScalarFunction(name='%s_start' % name, dtype=np.int32)
                    finish = ScalarFunction(name='%s_finish' % name, dtype=np.int32)
                    args.extend(zip([ccode(j) for j in bounds], (start, finish)))
                    # Iteration unbounded indices
                    ufunc = [ScalarFunction(name='%s_ub%d' % (name, j), dtype=np.int32)
                             for j in range(len(i.uindices))]
                    args.extend(zip([ccode(j.start) for j in i.uindices], ufunc))
                    limits = [Symbol(start.name), Symbol(finish.name), 1]
                    uindices = [UnboundedIndex(j.index, i.dim + as_symbol(k))
                                for j, k in zip(i.uindices, ufunc)]
                    free.append(i._rebuild(limits=limits, offsets=None,
                                           uindices=uindices))
                    not_required.update({i.dim}, set(j.index for j in i.uindices))

                # Construct elemental function body, and inspect it
                free = NestedTransformer(dict((zip(target, free)))).visit(root)
                expressions = FindNodes(Expression).visit(free)
                fsymbols = FindSymbols('symbolics').visit(free)

                # Retrieve tensor arguments
                for i in fsymbols:
                    if i.is_SymbolicFunction:
                        handle = "(%s*) %s" % (c.dtype_to_ctype(i.dtype), i.name)
                    else:
                        handle = "%s_vec" % i.name
                    args.append((handle, i))

                # Retrieve scalar arguments
                not_required.update({i.output for i in expressions if i.is_scalar})
                maybe_required.update(set(FindSymbols(mode='free-symbols').visit(free)))
                for i in fsymbols:
                    not_required.update({as_symbol(i)})
                    for j in i.symbolic_shape:
                        maybe_required.update(j.free_symbols)
                required = filter_sorted(maybe_required - not_required,
                                         key=attrgetter('name'))
                args.extend([(i.name, ScalarFunction(name=i.name, dtype=np.int32))
                             for i in required])

                call, params = zip(*args)
                handle = flatten([p.rtargs for p in params])
                name = "f_%d" % root.tag

                # Produce the new FunCall
                mapper[root] = List(header=noinline, body=FunCall(name, call))

                # Produce the new Function
                functions.setdefault(name,
                                     Function(name, free, 'void', handle, ('static',)))

            # Transform the main tree
            processed.append(Transformer(mapper).visit(node))

        return {'nodes': processed, 'elemental_functions': functions.values()}

    def _compiler_decoration(self, name, default=None):
        key = self.params['compiler'].__class__.__name__
        complang = complang_ALL.get(key, {})
        return complang.get(name, default)
