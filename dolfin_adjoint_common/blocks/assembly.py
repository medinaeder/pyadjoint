import ufl
from pyadjoint import Block, create_overloaded_object


class AssembleBlock(Block):
    def __init__(self, form):
        super(AssembleBlock, self).__init__()
        self.form = form
        if self.backend.__name__ != "firedrake":
            mesh = self.form.ufl_domain().ufl_cargo()
        else:
            mesh = self.form.ufl_domain()
        self.add_dependency(mesh)
        for c in self.form.coefficients():
            self.add_dependency(c, no_duplicates=True)

    def __str__(self):
        return str(self.form)

    def prepare_evaluate_adj(self, inputs, adj_inputs, relevant_dependencies):
        replaced_coeffs = {}
        for block_variable in self.get_dependencies():
            coeff = block_variable.output
            c_rep = block_variable.saved_output
            if coeff in self.form.coefficients():
                replaced_coeffs[coeff] = c_rep

        form = ufl.replace(self.form, replaced_coeffs)
        Nk = tuple(e for e in form.coefficients() if isinstance(e, ufl.ExternalOperator))

        prepared = {}
        prepared["form"] = form
        prepared["extops"] = Nk
        return prepared

    def evaluate_adj_component(self, inputs, adj_inputs, block_variable, idx, prepared=None):
        form = prepared["form"]
        Nk = prepared["extops"]
        adj_input = adj_inputs[0]
        c = block_variable.output
        c_rep = block_variable.saved_output

        if isinstance(c, self.compat.ExpressionType):
            # Create a FunctionSpace from self.form and Expression.
            # And then make a TestFunction from this space.
            mesh = self.form.ufl_domain().ufl_cargo()
            V = c._ad_function_space(mesh)
            dc = self.backend.TestFunction(V)

            dform = self.backend.derivative(form, c_rep, dc)
            output = self.compat.assemble_adjoint_value(dform)
            return [[adj_input * output, V]]
        elif isinstance(c, self.compat.MeshType):
            X = self.backend.SpatialCoordinate(c_rep)
            dform = self.backend.derivative(form, X)
            output = self.compat.assemble_adjoint_value(dform)
            return adj_input * output

        if isinstance(c, self.backend.Function):
            fct_space = c.function_space()
            dc = self.backend.TestFunction(fct_space)
        elif isinstance(c, self.backend.Constant):
            mesh = self.compat.extract_mesh_from_form(self.form)
            fct_space = c._ad_function_space(mesh)
            dc = self.backend.TestFunction(fct_space)

        if c_rep not in Nk:
            c_substitute = self.backend.Function(fct_space)
            if isinstance(c_rep, self.backend.Constant):
                c_substitute = self.backend.Constant(0.)
            Nk_rep = tuple(self.backend.replace(e, {c_rep: c_substitute}) for e in Nk)
            form_Nk_rep = self.backend.replace(form, dict(zip(Nk, Nk_rep)))
            dform = self.backend.derivative(form_Nk_rep, c_rep, dc)
        else:
            # Reconstruct c_rep operands with saved outputs
            c_rep = c_rep._ufl_expr_reconstruct_(*tuple(e.block_variable.saved_output for e in c_rep.ufl_operands))
            dform = self.backend.derivative(form, c_rep, dc)

        output = self.compat.assemble_adjoint_value(dform)
        return adj_input * output

    def prepare_evaluate_tlm(self, inputs, tlm_inputs, relevant_outputs):
        return self.prepare_evaluate_adj(inputs, tlm_inputs, self.get_dependencies())

    def evaluate_tlm_component(self, inputs, tlm_inputs, block_variable, idx, prepared=None):
        form = prepared["form"]
        dform = 0.
        dform_shape = 0.
        for bv in self.get_dependencies():
            c_rep = bv.saved_output
            tlm_value = bv.tlm_value

            if tlm_value is None:
                continue
            if isinstance(c_rep, self.compat.MeshType):
                X = self.backend.SpatialCoordinate(c_rep)
                dform_shape += self.compat.assemble_adjoint_value(
                    self.backend.derivative(form, X, tlm_value))
            else:
                dform += self.backend.derivative(form, c_rep, tlm_value)
        if not isinstance(dform, float):
            dform = self.compat.assemble_adjoint_value(dform)
        return dform + dform_shape

    def prepare_evaluate_hessian(self, inputs, hessian_inputs, adj_inputs, relevant_dependencies):
        return self.prepare_evaluate_adj(inputs, adj_inputs, relevant_dependencies)

    def evaluate_hessian_component(self, inputs, hessian_inputs, adj_inputs, block_variable, idx,
                                   relevant_dependencies, prepared=None):
        form = prepared["form"]
        hessian_input = hessian_inputs[0]
        adj_input = adj_inputs[0]

        c1 = block_variable.output
        c1_rep = block_variable.saved_output

        if isinstance(c1, self.backend.Function):
            dc = self.backend.TestFunction(c1.function_space())
        elif isinstance(c1, self.compat.ExpressionType):
            mesh = form.ufl_domain().ufl_cargo()
            W = c1._ad_function_space(mesh)
            dc = self.backend.TestFunction(W)
        elif isinstance(c1, self.backend.Constant):
            mesh = self.compat.extract_mesh_from_form(form)
            dc = self.backend.TestFunction(c1._ad_function_space(mesh))
        elif isinstance(c1, self.compat.MeshType):
            pass
        else:
            return None

        if isinstance(c1, self.compat.MeshType):
            X = self.backend.SpatialCoordinate(c1)
            dform = self.backend.derivative(form, X)
        else:
            dform = self.backend.derivative(form, c1_rep, dc)
        hessian_outputs = hessian_input * self.compat.assemble_adjoint_value(dform)

        for other_idx, bv in relevant_dependencies:
            c2_rep = bv.saved_output
            tlm_input = bv.tlm_value

            if tlm_input is None:
                continue

            if isinstance(c2_rep, self.compat.MeshType):
                X = self.backend.SpatialCoordinate(c2_rep)
                ddform = self.backend.derivative(dform, X, tlm_input)
            else:
                ddform = self.backend.derivative(dform, c2_rep, tlm_input)
            hessian_outputs += adj_input * self.compat.assemble_adjoint_value(ddform)

        if isinstance(c1, self.compat.ExpressionType):
            return [(hessian_outputs, W)]
        else:
            return hessian_outputs

    def prepare_recompute_component(self, inputs, relevant_outputs):
        return self.prepare_evaluate_adj(inputs, None, None)

    def recompute_component(self, inputs, block_variable, idx, prepared):
        form = prepared["form"]
        output = self.backend.assemble(form)
        output = create_overloaded_object(output)
        return output
