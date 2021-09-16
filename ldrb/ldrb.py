import logging
from collections import namedtuple
from typing import Dict, Iterable, Optional

import dolfin as df
import numpy as np
import quaternion
from dolfin.mesh.meshfunction import MeshFunction
from scipy import optimize

from . import utils

FiberSheetSystem = namedtuple("FiberSheetSystem", "fiber, sheet, sheet_normal")


def normalize(u: np.ndarray) -> np.ndarray:
    """
    Normalize vector
    """
    return u / np.linalg.norm(u)


def axis(u: np.ndarray, v: np.ndarray) -> np.ndarray:
    r"""
    Construct the fiber orientation coordinate system.

    Given two vectors :math:`u` and :math:`v` in the apicobasal
    and transmural direction respectively return a matrix that
    represents an orthonormal basis in the circumferential (first),
    apicobasal (second) and transmural (third) direction.
    """

    e1 = normalize(u)

    # Create an initial guess for e0
    e2 = normalize(v)
    e2 -= e1.dot(e2) * e1
    e2 = normalize(e2)

    e0 = np.cross(e1, e2)
    e0 = normalize(e0)

    def f(e0):
        e2 = normalize(v - e0.dot(v) * e0)
        return e0 - np.cross(e1, e2)

    sol = optimize.root(f, e0)
    e0 = sol.x
    e2 = normalize(v - e0.dot(v) * e0)

    Q = np.array([e0, e1, e2]).T
    return Q


def orient(Q: np.ndarray, alpha: float, beta: float) -> np.ndarray:
    r"""
    Define the orthotropic fiber orientations.

    Given a coordinate system :math:`Q`, in the canonical
    basis, rotate it in order to align with the fiber, sheet
    and sheet-normal axis determine by the angles :math:`\alpha`
    (fiber) and :math:`\beta` (sheets).
    """
    A = np.array(
        [
            [np.cos(np.radians(alpha)), -np.sin(np.radians(alpha)), 0],
            [np.sin(np.radians(alpha)), np.cos(np.radians(alpha)), 0],
            [0, 0, 1],
        ]
    )

    B = np.array(
        [
            [1, 0, 0],
            [0, np.cos(np.radians(beta)), np.sin(np.radians(beta))],
            [0, -np.sin(np.radians(beta)), np.cos(np.radians(beta))],
        ]
    )

    C = np.dot(Q.real, A).dot(B)
    return C


def laplace(
    mesh: df.Mesh,
    fiber_space: str,
    markers: Optional[Dict[str, int]],
    ffun: Optional[df.MeshFunction] = None,
) -> Dict[str, np.ndarray]:
    """
    Solve the laplace equation and project the gradients
    of the solutions.
    """

    # Create scalar laplacian solutions
    df.info("Calculating scalar fields")
    scalar_solutions = scalar_laplacians(mesh, markers, ffun=ffun)

    # Create gradients
    df.info("\nCalculating gradients")
    data = project_gradients(mesh, fiber_space, scalar_solutions)

    return data


def bislerp(
    Qa: Optional[np.ndarray], Qb: Optional[np.ndarray], t: float
) -> Optional[np.ndarray]:
    r"""
    Linear interpolation of two orthogonal matrices.
    Assiume that :math:`Q_a` and :math:`Q_b` refers to
    timepoint :math:`0` and :math:`1` respectively.
    Using spherical linear interpolation (slerp) find the
    orthogonal matrix at timepoint :math:`t`.
    """

    if Qa is None and Qb is None:
        return None
    if Qa is None:
        return Qb
    if Qb is None:
        return Qa

    tol = 1e-12
    qa = quaternion.from_rotation_matrix(Qa)
    qb = quaternion.from_rotation_matrix(Qb)

    quat_i = quaternion.quaternion(0, 1, 0, 0)
    quat_j = quaternion.quaternion(0, 0, 1, 0)
    quat_k = quaternion.quaternion(0, 0, 0, 1)

    quat_array = [
        qa,
        -qa,
        qa * quat_i,
        -qa * quat_i,
        qa * quat_j,
        -qa * quat_j,
        qa * quat_k,
        -qa * quat_k,
    ]

    def dot(qi, qj):
        return np.sum([getattr(qi, s) * getattr(qj, s) for s in ["x", "y", "z", "w"]])

    dot_arr = [abs(dot(qi, qb)) for qi in quat_array]
    max_idx = int(np.argmax(dot_arr))
    max_dot = dot_arr[max_idx]
    qm = quat_array[max_idx]

    if max_dot > 1 - tol:
        return Qb

    qm_slerp = quaternion.slerp(qm, qb, 0, 1, t)
    qm_norm = qm_slerp.normalized()
    Qab = quaternion.as_rotation_matrix(qm_norm)
    return Qab


def standard_dofs(n: int) -> Iterable:
    """
    Get the standard list of dofs for a given length
    """

    x_dofs = np.arange(0, 3 * n, n)
    y_dofs = np.arange(1, 3 * n, n)
    z_dofs = np.arange(2, 3 * n, n)
    scalar_dofs = np.arange(0, n)
    return zip(x_dofs, y_dofs, z_dofs, scalar_dofs)


def system_at_dof(
    lv: float,
    rv: float,
    epi: float,
    grad_lv: np.ndarray,
    grad_rv: np.ndarray,
    grad_epi: np.ndarray,
    grad_ab: np.ndarray,
    alpha_endo: float,
    alpha_epi: float,
    beta_endo: float,
    beta_epi: float,
    tol: float = 1e-7,
) -> Optional[np.ndarray]:
    """
    Compte the fiber, sheet and sheet normal at a
    single degre of freedom

    Arguments
    ---------
    lv : float
        Value of the Laplace solution for the LV at the dof.
    rv : float
        Value of the Laplace solution for the RV at the dof.
    epi : float
        Value of the Laplace solution for the EPI at the dof.
    grad_lv : np.ndarray
        Gradient of the Laplace solution for the LV at the dof.
    grad_rv : np.ndarray
        Gradient of the Laplace solution for the RV at the dof.
    grad_epi : np.ndarray
        Gradient of the Laplace solution for the EPI at the dof.
    grad_epi : np.ndarray
        Gradient of the Laplace solution for the apex to base.
        at the dof
    alpha_endo : scalar
        Fiber angle at the endocardium.
    alpha_epi : scalar
        Fiber angle at the epicardium.
    beta_endo : scalar
        Sheet angle at the endocardium.
    beta_epi : scalar
        Sheet angle at the epicardium.
    tol : scalar
        Tolerance for whether to consider the scalar values.
        Default: 1e-7
    """

    alpha_s = lambda d: alpha_endo * (1 - d) - alpha_endo * d
    alpha_w = lambda d: alpha_endo * (1 - d) + alpha_epi * d
    beta_s = lambda d: beta_endo * (1 - d) - beta_endo * d
    beta_w = lambda d: beta_endo * (1 - d) + beta_epi * d

    if lv + rv < tol:
        depth = 0.5
    else:
        depth = rv / (lv + rv)

    Q_lv = None
    if lv > tol:
        Q_lv = axis(grad_ab, -1 * grad_lv)
        Q_lv = orient(Q_lv, alpha_s(depth), beta_s(depth))

    Q_rv = None
    if rv > tol:
        Q_rv = axis(grad_ab, grad_rv)
        Q_rv = orient(Q_rv, alpha_s(depth), beta_s(depth))

    Q_endo = bislerp(Q_lv, Q_rv, depth)

    Q_epi = None
    if epi > tol:
        Q_epi = axis(grad_ab, grad_epi)
        Q_epi = orient(Q_epi, alpha_w(epi), beta_w(epi))

    Q_fiber = bislerp(Q_endo, Q_epi, epi)
    return Q_fiber


def compute_fiber_sheet_system(
    lv_scalar: np.ndarray,
    lv_gradient: np.ndarray,
    epi_scalar: np.ndarray,
    epi_gradient: np.ndarray,
    apex_gradient: np.ndarray,
    dofs: Optional[Iterable] = None,
    rv_scalar: Optional[np.ndarray] = None,
    rv_gradient: Optional[np.ndarray] = None,
    alpha_endo_lv: float = 40,
    alpha_epi_lv: float = -50,
    alpha_endo_rv: Optional[float] = None,
    alpha_epi_rv: Optional[float] = None,
    alpha_endo_sept: Optional[float] = None,
    alpha_epi_sept: Optional[float] = None,
    beta_endo_lv: float = -65,
    beta_epi_lv: float = 25,
    beta_endo_rv: Optional[float] = None,
    beta_epi_rv: Optional[float] = None,
    beta_endo_sept: Optional[float] = None,
    beta_epi_sept: Optional[float] = None,
) -> FiberSheetSystem:
    """
    Compute the fiber-sheets system on all degrees of freedom.
    """
    if dofs is None:
        dofs = standard_dofs(len(lv_scalar))
    if rv_scalar is None:
        rv_scalar = np.zeros_like(lv_scalar)
    if rv_gradient is None:
        rv_gradient = np.zeros_like(lv_gradient)

    alpha_endo_rv = alpha_endo_rv or alpha_endo_lv
    alpha_epi_rv = alpha_epi_rv or alpha_epi_lv
    alpha_endo_sept = alpha_endo_sept or alpha_endo_lv
    alpha_epi_sept = alpha_epi_sept or alpha_epi_lv

    beta_endo_rv = beta_endo_rv or beta_endo_lv
    beta_epi_rv = beta_epi_rv or beta_epi_lv
    beta_endo_sept = beta_endo_sept or beta_epi_lv
    beta_epi_sept = beta_epi_sept or beta_epi_lv

    df.info("Compute fiber-sheet system")
    df.info("Angles: ")
    df.info(
        (
            "alpha: "
            "\n endo_lv: {endo_lv}"
            "\n epi_lv: {epi_lv}"
            "\n endo_septum: {endo_sept}"
            "\n epi_septum: {epi_sept}"
            "\n endo_rv: {endo_rv}"
            "\n epi_rv: {epi_rv}"
            ""
        ).format(
            endo_lv=alpha_endo_lv,
            epi_lv=alpha_epi_lv,
            endo_sept=alpha_endo_sept,
            epi_sept=alpha_epi_sept,
            endo_rv=alpha_endo_rv,
            epi_rv=alpha_epi_rv,
        )
    )
    df.info(
        (
            "beta: "
            "\n endo_lv: {endo_lv}"
            "\n epi_lv: {epi_lv}"
            "\n endo_septum: {endo_sept}"
            "\n epi_septum: {epi_sept}"
            "\n endo_rv: {endo_rv}"
            "\n epi_rv: {epi_rv}"
            ""
        ).format(
            endo_lv=beta_endo_lv,
            epi_lv=beta_epi_lv,
            endo_sept=beta_endo_sept,
            epi_sept=beta_epi_sept,
            endo_rv=beta_endo_rv,
            epi_rv=beta_epi_rv,
        )
    )

    f0 = np.zeros_like(lv_gradient)
    s0 = np.zeros_like(lv_gradient)
    n0 = np.zeros_like(lv_gradient)

    tol = 1e-3

    for (x_dof, y_dof, z_dof, s_dof) in dofs:

        dof = np.array([x_dof, y_dof, z_dof])

        lv = lv_scalar[s_dof]
        rv = rv_scalar[s_dof]
        epi = epi_scalar[s_dof]
        grad_lv = lv_gradient[dof]
        grad_rv = rv_gradient[dof]
        grad_epi = epi_gradient[dof]
        grad_ab = apex_gradient[dof]

        # df.debug("LV: {lv:.2f}, RV: {rv:.2f}, EPI: {epi:.2f}".format(lv=lv,
        #                                                              rv=rv,
        #                                                              epi=epi))

        if lv > tol and rv < tol:
            # We are in the LV region
            alpha_endo = alpha_endo_lv
            beta_endo = beta_endo_lv
            alpha_epi = alpha_epi_lv
            beta_epi = beta_epi_lv
        elif lv < tol and rv > tol:
            # We are in the RV region
            alpha_endo = alpha_endo_rv
            beta_endo = beta_endo_rv
            alpha_epi = alpha_epi_rv
            beta_epi = beta_epi_rv
        elif lv > tol and rv > tol:
            # We are in the septum
            alpha_endo = alpha_endo_sept
            beta_endo = beta_endo_sept
            alpha_epi = alpha_epi_sept
            beta_epi = beta_epi_sept
        else:
            pass
            # We are at the epicardium somewhere
            # msg = (
            #     "Unable to determine region. "
            #     "LV: {lv:.2f}, RV: {rv:.2f}, EPI: {epi:.2f}.\n"
            #     "Will use LV angles in this regions. "
            #     "Microstructure might be broken at this "
            #     "point"
            # ).format(lv=lv, rv=rv, epi=epi)
            # df.debug(msg)

            alpha_endo = alpha_endo_lv
            beta_endo = beta_endo_lv
            alpha_epi = alpha_epi_lv
            beta_epi = beta_epi_lv

        Q_fiber = system_at_dof(
            lv=lv,
            rv=rv,
            epi=epi,
            grad_lv=grad_lv,
            grad_rv=grad_rv,
            grad_epi=grad_epi,
            grad_ab=grad_ab,
            alpha_endo=alpha_endo,
            alpha_epi=alpha_epi,
            beta_endo=beta_endo,
            beta_epi=beta_epi,
            tol=tol,
        )
        if Q_fiber is None:
            df.info(f"Invalid system at dof {dof}")
            continue
        f0[dof] = Q_fiber.T[0]
        s0[dof] = Q_fiber.T[1]
        n0[dof] = Q_fiber.T[2]

    return FiberSheetSystem(fiber=f0, sheet=s0, sheet_normal=n0)


def dofs_from_function_space(mesh: df.Mesh, fiber_space: str) -> Iterable:
    """
    Get the dofs from a function spaces define in the
    fiber_space string.
    """
    Vv = utils.space_from_string(fiber_space, mesh, dim=3)
    V = utils.space_from_string(fiber_space, mesh, dim=1)
    # Get dofs
    dim = Vv.mesh().geometry().dim()
    start, end = Vv.sub(0).dofmap().ownership_range()
    x_dofs = np.arange(0, end - start, dim)
    y_dofs = np.arange(1, end - start, dim)
    z_dofs = np.arange(2, end - start, dim)

    start, end = V.dofmap().ownership_range()
    scalar_dofs = [
        dof
        for dof in range(end - start)
        if V.dofmap().local_to_global_index(dof)
        not in V.dofmap().local_to_global_unowned()
    ]

    return zip(x_dofs, y_dofs, z_dofs, scalar_dofs)


def dolfin_ldrb(
    mesh: df.Mesh,
    fiber_space: str = "CG_1",
    ffun: Optional[df.MeshFunction] = None,
    markers: Optional[Dict[str, int]] = None,
    log_level: int = logging.INFO,
    **angles: Optional[float],
):
    r"""
    Create fiber, cross fibers and sheet directions

    Arguments
    ---------
    mesh : dolfin.Mesh
        The mesh
    fiber_space : str
        A string on the form {familiy}_{degree} which
        determines for what space the fibers should be calculated for.
        If not provdied, then a first order Lagrange space will be used,
        i.e Lagrange_1.
    ffun : dolfin.MeshFunctionSizet (optional)
        A facet function containing markers for the boundaries.
        If not provided, the markers stored within the mesh will
        be used.
    markers : dict (optional)
        A dictionary with the markers for the
        different bondaries defined in the facet function
        or within the mesh itself.
        The follwing markers must be provided:
        'base', 'lv', 'epi, 'rv' (optional).
        If the markers are not provided the following default
        vales will be used: base = 10, rv = 20, lv = 30, epi = 40
    log_level : int
        How much to print. DEBUG=10, INFO=20, WARNING=30.
        Default: INFO
    angles : kwargs
        Keyword arguments with the fiber and sheet angles.
        It is possible to set different angles on the LV,
        RV and septum, however it either the RV or septum
        angles are not provided, then the angles on the LV
        will be used. The default values are taken from the
        original paper, namely

        .. math::

            \alpha_{\text{endo}} &= 40 \\
            \alpha_{\text{epi}} &= -50 \\
            \beta_{\text{endo}} &= -65 \\
            \beta_{\text{epi}} &= 25

        The following keyword arguments are possible:

        alpha_endo_lv : scalar
            Fiber angle at the LV endocardium.
        alpha_epi_lv : scalar
            Fiber angle at the LV epicardium.
        beta_endo_lv : scalar
            Sheet angle at the LV endocardium.
        beta_epi_lv : scalar
            Sheet angle at the LV epicardium.
        alpha_endo_rv : scalar
            Fiber angle at the RV endocardium.
        alpha_epi_rv : scalar
            Fiber angle at the RV epicardium.
        beta_endo_rv : scalar
            Sheet angle at the RV endocardium.
        beta_epi_rv : scalar
            Sheet angle at the RV epicardium.
        alpha_endo_sept : scalar
            Fiber angle at the septum endocardium.
        alpha_epi_sept : scalar
            Fiber angle at the septum epicardium.
        beta_endo_sept : scalar
            Sheet angle at the septum endocardium.
        beta_epi_sept : scalar
            Sheet angle at the septum epicardium.


    """
    log_level = df.get_log_level()
    df.set_log_level(logging.INFO)

    if not isinstance(mesh, df.Mesh):
        raise TypeError("Expected a dolfin.Mesh as the mesh argument.")

    if ffun is not None:
        utils.mark_facets(mesh, ffun)

    # Solve the Laplace-Dirichlet problem
    data = laplace(mesh, fiber_space, markers)

    dofs = dofs_from_function_space(mesh, fiber_space)

    system = compute_fiber_sheet_system(dofs=dofs, **data, **angles)  # type:ignore

    df.set_log_level(log_level)
    return fiber_system_to_dolfin(system, mesh, fiber_space)


def fiber_system_to_dolfin(
    system: FiberSheetSystem, mesh: df.Mesh, fiber_space: str
) -> FiberSheetSystem:
    """
    Convert fiber-sheet system of numpy arrays to dolfin
    functions.
    """
    Vv = utils.space_from_string(fiber_space, mesh, dim=3)

    f0 = df.Function(Vv)
    f0.vector().set_local(system.fiber)
    f0.vector().apply("insert")
    f0.rename("fiber", "fibers")

    s0 = df.Function(Vv)
    s0.vector().set_local(system.sheet)
    s0.vector().apply("insert")
    s0.rename("sheet", "fibers")

    n0 = df.Function(Vv)
    n0.vector().set_local(system.sheet_normal)
    n0.vector().apply("insert")
    n0.rename("sheet_normal", "fibers")

    return FiberSheetSystem(fiber=f0, sheet=s0, sheet_normal=n0)


def apex_to_base(
    mesh: df.Mesh, base_marker: int, ffun: Optional[df.MeshFunction] = None
):
    """
    Find the apex coordinate and compute the laplace
    equation to find the apex to base solution

    Arguments
    ---------
    mesh : dolfin.Mesh
        The mesh
    base_marker : int
        The marker value for the basal facets
    ffun : dolfin.MeshFunctionSizet (optional)
        A facet function containing markers for the boundaries.
        If not provided, the markers stored within the mesh will
        be used.
    """
    # Find apex by solving a laplacian with base solution = 0
    # Create Base variational problem
    V = df.FunctionSpace(mesh, "CG", 1)

    u = df.TrialFunction(V)
    v = df.TestFunction(V)

    a = df.dot(df.grad(u), df.grad(v)) * df.dx
    L = v * df.Constant(1) * df.dx

    apex = df.Function(V)

    base_bc = df.DirichletBC(V, 1, ffun, base_marker, "topological")

    def solve(solver_parameters):
        df.solve(a == L, apex, base_bc, solver_parameters=solver_parameters)

    solver_parameters = {"linear_solver": "cg", "preconditioner": "amg"}
    pcs = (pc for pc in ["petsc_amg", "default"])
    while 1:
        try:
            solve(solver_parameters)
        except RuntimeError:
            solver_parameters["preconditioner"] = next(pcs)
        else:
            break

    if utils.DOLFIN_VERSION_MAJOR < 2018:
        dof_x = utils.gather_broadcast(V.tabulate_dof_coordinates()).reshape((-1, 3))
        apex_values = utils.gather_broadcast(apex.vector().get_local())
        ind = apex_values.argmax()
        apex_coord = dof_x[ind]
    else:
        dof_x = V.tabulate_dof_coordinates()
        apex_values = apex.vector().get_local()
        local_max_val = apex_values.max()

        local_apex_coord = dof_x[apex_values.argmax()]
        comm = utils.mpi_comm_world()

        from mpi4py import MPI

        global_max, apex_coord = comm.allreduce(
            sendobj=(local_max_val, local_apex_coord), op=MPI.MAXLOC
        )

    df.info("  Apex coord: ({0:.2f}, {1:.2f}, {2:.2f})".format(*apex_coord))

    # Update rhs
    L = v * df.Constant(0) * df.dx
    apex_domain = df.CompiledSubDomain(
        "near(x[0], {0}) && near(x[1], {1}) && near(x[2], {2})".format(*apex_coord)
    )
    apex_bc = df.DirichletBC(V, 0, apex_domain, "pointwise")

    # Solve the poisson equation
    df.solve(
        a == L, apex, [base_bc, apex_bc], solver_parameters={"linear_solver": "gmres"}
    )

    return apex


def project_gradients(
    mesh: df.Mesh, fiber_space: str, scalar_solutions: Dict[str, df.Function]
) -> Dict[str, np.ndarray]:
    """
    Calculate the gradients using projections

    Arguments
    ---------
    mesh : dolfin.Mesh
        The mesh
    fiber_space : str
        A string on the form {familiy}_{degree} which
        determines for what space the fibers should be calculated for.
    scalar_solutions: dict
        A dictionary with the scalar solutions that you
        want to compute the gradients of.
    """
    Vv = utils.space_from_string(fiber_space, mesh, dim=3)
    V = utils.space_from_string(fiber_space, mesh, dim=1)

    data = {}
    V_cg = df.FunctionSpace(mesh, df.VectorElement("Lagrange", mesh.ufl_cell(), 1))
    for case, scalar_solution in scalar_solutions.items():

        gradient_cg = df.project(df.grad(scalar_solution), V_cg, solver_type="cg")
        gradient = df.interpolate(gradient_cg, Vv)
        scalar_solution = df.interpolate(scalar_solution, V)

        # Add scalar data
        if case != "apex":
            data[case + "_scalar"] = scalar_solution.vector().get_local()
        # Add gradient data
        data[case + "_gradient"] = gradient.vector().get_local()

    # Return data
    return data


def scalar_laplacians(
    mesh: df.Mesh,
    markers: Optional[Dict[str, int]] = None,
    ffun: Optional[MeshFunction] = None,
) -> Dict[str, df.Function]:
    """
    Calculate the laplacians

    Arguments
    ---------
    mesh : dolfin.Mesh
       A dolfin mesh
    markers : dict (optional)
        A dictionary with the markers for the
        different bondaries defined in the facet function
        or within the mesh itself.
        The follwing markers must be provided:
        'base', 'lv', 'epi, 'rv' (optional).
        If the markers are not provided the following default
        vales will be used: base = 10, rv = 20, lv = 30, epi = 40.
    fiber_space : str
        A string on the form {familiy}_{degree} which
        determines for what space the fibers should be calculated for.
    """

    if not isinstance(mesh, df.Mesh):
        raise TypeError("Expected a dolfin.Mesh as the mesh argument.")

    # Init connectivities
    mesh.init(2)
    if ffun is None:
        ffun = df.MeshFunction("size_t", mesh, 2, mesh.domains())

    # Boundary markers, solutions and cases
    if markers is None:
        markers = utils.default_markers()
    else:

        keys = ["base", "lv", "epi"]
        msg = (
            "Key {key} not found in markers. Make sure to provide a"
            "key-value pair for {keys}"
        )
        for key in keys:
            assert key in markers, msg.format(key=key, keys=keys)
        if "rv" not in markers:
            df.info("No marker for the RV found. Asssume this is an LV geometry")
            rv_value = 20
            # Just make sure that this value is not used for any of the other boundaries.
            while rv_value in markers.values():
                rv_value += 1
            markers["rv"] = rv_value

    markers_str = "\n".join(["{}: {}".format(k, v) for k, v in markers.items()])
    df.info(
        ("Compute scalar laplacian solutions with the markers: \n" "{}").format(
            markers_str
        )
    )

    cases = ["rv", "lv", "epi"]
    boundaries = cases + ["base"]

    # Check that all boundary faces are marked
    num_boundary_facets = df.BoundaryMesh(mesh, "exterior").num_cells()
    if num_boundary_facets != sum(
        [np.sum(ffun.array() == markers[boundary]) for boundary in boundaries]
    ):

        df.error(
            (
                "Not all boundary faces are marked correctly. Make sure all "
                "boundary facets are marked as: {}"
                ""
            ).format(", ".join(["{} = {}".format(k, v) for k, v in markers.items()]))
        )

    # Compte the apex to base solutons
    apex = apex_to_base(mesh, markers["base"], ffun)

    # Find the rest of the laplace soltions
    V = apex.function_space()
    u = df.TrialFunction(V)
    v = df.TestFunction(V)

    a = df.dot(df.grad(u), df.grad(v)) * df.dx
    L = v * df.Constant(0) * df.dx
    solutions = dict((what, df.Function(V)) for what in cases)
    solutions["apex"] = apex

    df.info("  Num coords: {0}".format(mesh.num_vertices()))
    df.info("  Num cells: {0}".format(mesh.num_cells()))

    # solver_param = dict(
    #     solver_parameters=dict(
    #         preconditioner="ml_amg"
    #         if df.has_krylov_solver_preconditioner("ml_amg")
    #         else "default",
    #         linear_solver="gmres",
    #     )
    # )
    if "superlu_dist" in df.linear_solver_methods():
        solver_param = dict(
            solver_parameters=dict(
                linear_solver="superlu_dist",
            )
        )
    else:
        solver_param = {}

    # Check that solution of the three last cases all sum to 1.
    sol = solutions["apex"].vector().copy()
    sol[:] = 0.0

    if len(ffun.array()[ffun.array() == markers["rv"]]) == 0:
        # Remove the RV
        cases.pop(next(i for i, c in enumerate(cases) if c == "rv"))

    # Iterate over the three different cases
    df.info("Solving Laplace equation")
    for case in cases:
        df.info(
            " {0} = 1, {1} = 0".format(case, ", ".join([c for c in cases if c != case]))
        )
        # Solve linear system
        bcs = [
            df.DirichletBC(
                V, 1 if what == case else 0, ffun, markers[what], "topological"
            )
            for what in cases
        ]
        df.solve(a == L, solutions[case], bcs, **solver_param)

        # Enforce bound on solution:
        solutions[case].vector()[
            solutions[case].vector() < df.DOLFIN_EPS
        ] = df.DOLFIN_EPS
        solutions[case].vector()[solutions[case].vector() > 1.0 - df.DOLFIN_EPS] = (
            1.0 - df.DOLFIN_EPS
        )

        sol += solutions[case].vector()

    assert np.all(sol > 0.999), "Sum not 1..."

    # Return the solutions
    return solutions
