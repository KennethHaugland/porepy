# -*- coding: utf-8 -*-
"""

@author: fumagalli, alessio
"""

import warnings
import numpy as np
import scipy.sparse as sps
import scipy.linalg as linalg
import logging

import porepy as pp

from porepy.numerics.mixed_dim.solver import Solver
from porepy.numerics.mixed_dim.coupler import Coupler
from porepy.numerics.mixed_dim.abstract_coupling import AbstractCoupling
from porepy.numerics.vem import DualMixedDim, DualCoupling

# Module-wide logger
logger = logging.getLogger(__name__)

# ------------------------------------------------------------------------------#


class RT0MixedDim(DualMixedDim):
    def __init__(self, physics="flow"):
        self.physics = physics

        self.discr = RT0(self.physics)
        self.discr_ndof = self.discr.ndof
        self.coupling_conditions = DualCoupling(self.discr)

        self.solver = Coupler(self.discr, self.coupling_conditions)


# ------------------------------------------------------------------------------#


class RT0(Solver):

    # ------------------------------------------------------------------------------#

    def __init__(self, physics="flow"):
        self.physics = physics

    # ------------------------------------------------------------------------------#

    def ndof(self, g):
        """
        Return the number of degrees of freedom associated to the method.
        In this case number of faces (velocity dofs) plus the number of cells
        (pressure dof). If a mortar grid is given the number of dof are equal to
        the number of cells, we are considering an inter-dimensional interface
        with flux variable as mortars.


        Parameter
        ---------
        g: grid, or a subclass.

        Return
        ------
        dof: the number of degrees of freedom.

        """
        if isinstance(g, pp.Grid):
            return g.num_cells + g.num_faces
        elif isinstance(g, pp.MortarGrid):
            return g.num_cells
        else:
            raise ValueError

    # ------------------------------------------------------------------------------#

    def matrix_rhs(self, g, data):
        """
        Return the matrix and righ-hand side for a discretization of a second
        order elliptic equation using RT0-P0 method.

        Parameters
        ----------
        g : grid, or a subclass, with geometry fields computed.
        data: dictionary to store the data.

        Return
        ------
        matrix: sparse csr (g.num_faces+g_num_cells, g.num_faces+g_num_cells)
            Saddle point matrix obtained from the discretization.
        rhs: array (g.num_faces+g_num_cells)
            Right-hand side which contains the boundary conditions and the scalar
            source term.

        """
        M, bc_weight = self.matrix(g, data, bc_weight=True)
        return M, self.rhs(g, data, bc_weight)

    # ------------------------------------------------------------------------------#

    def matrix(self, g, data, bc_weight=False):
        """
        Return the matrix for a discretization of a second order elliptic equation
        using RT0-P0 method. See self.matrix_rhs for a detaild
        description.

        Additional parameter:
        --------------------
        bc_weight: to compute the infinity norm of the matrix and use it as a
            weight to impose the boundary conditions. Default True.

        Additional return:
        weight: if bc_weight is True return the weight computed.

        """
        # Allow short variable names in backend function
        # pylint: disable=invalid-name

        # If a 0-d grid is given then we return an identity matrix
        if g.dim == 0:
            M = sps.dia_matrix(([1, 0], 0), (self.ndof(g), self.ndof(g)))
            if bc_weight:
                return M, 1
            return M

        # Retrieve the permeability, boundary conditions, and aperture
        # The aperture is needed in the hybrid-dimensional case, otherwise is
        # assumed unitary
        param = data["param"]
        k = param.get_tensor(self)
        bc = param.get_bc(self)
        a = param.get_aperture()

        faces, cells, sign = sps.find(g.cell_faces)
        index = np.argsort(cells)
        faces, sign = faces[index], sign[index]

        # Map the domain to a reference geometry (i.e. equivalent to compute
        # surface coordinates in 1d and 2d)
        c_centers, f_normals, f_centers, R, dim, node_coords = pp.cg.map_grid(g)

        if not data.get("is_tangential", False):
            # Rotate the permeability tensor and delete last dimension
            if g.dim < 3:
                k = k.copy()
                k.rotate(R)
                remove_dim = np.where(np.logical_not(dim))[0]
                k.perm = np.delete(k.perm, (remove_dim), axis=0)
                k.perm = np.delete(k.perm, (remove_dim), axis=1)

        # Allocate the data to store matrix entries, that's the most efficient
        # way to create a sparse matrix.
        size = np.power(g.dim + 1, 2) * g.num_cells
        I = np.empty(size, dtype=np.int)
        J = np.empty(size, dtype=np.int)
        dataIJ = np.empty(size)
        idx = 0

        nodes, _, _ = sps.find(g.face_nodes)

        size_HB = g.dim * (g.dim + 1)
        HB = np.zeros((size_HB, size_HB))
        for it in np.arange(0, size_HB, g.dim):
            HB += np.diagflat(np.ones(size_HB - it), it)
        HB += HB.T
        HB /= g.dim * g.dim * (g.dim + 1) * (g.dim + 2)

        for c in np.arange(g.num_cells):
            # For the current cell retrieve its faces
            loc = slice(g.cell_faces.indptr[c], g.cell_faces.indptr[c + 1])
            faces_loc = faces[loc]

            # find the opposite node id for each face
            node = self.opposite_side_node(g.face_nodes, nodes, faces_loc)
            coord_loc = node_coords[:, node]

            # Compute the H_div-mass local matrix
            A = self.massHdiv(
                a[c] * k.perm[0 : g.dim, 0 : g.dim, c],
                g.cell_volumes[c],
                coord_loc,
                sign[loc],
                g.dim,
                HB,
            )

            # Save values for Hdiv-mass local matrix in the global structure
            cols = np.tile(faces_loc, (faces_loc.size, 1))
            loc_idx = slice(idx, idx + cols.size)
            I[loc_idx] = cols.T.ravel()
            J[loc_idx] = cols.ravel()
            dataIJ[loc_idx] = A.ravel()
            idx += cols.size

        # Construct the global matrices
        mass = sps.coo_matrix((dataIJ, (I, J)))
        div = -g.cell_faces.T
        M = sps.bmat([[mass, div.T], [div, None]], format="csr")

        norm = sps.linalg.norm(mass, np.inf) if bc_weight else 1

        # For dual discretizations, internal boundaries
        # are handled by assigning Dirichlet conditions. Thus, we remove them
        # from the is_neu and is_rob (where they belong by default) and add them in
        # is_dir.

        # assign the Neumann boundary conditions
        is_neu = np.logical_and(bc.is_neu, np.logical_not(bc.is_internal))
        if bc and np.any(is_neu):
            # it is assumed that the faces dof are put before the cell dof
            is_neu = np.where(is_neu)[0]

            # set in an efficient way the essential boundary conditions, by
            # clear the rows and put norm in the diagonal
            for row in is_neu:
                M.data[M.indptr[row] : M.indptr[row + 1]] = 0.

            d = M.diagonal()
            d[is_neu] = norm
            M.setdiag(d)

        # assign the Robin boundary conditions
        is_rob = np.logical_and(bc.is_rob, np.logical_not(bc.is_internal))
        if bc and np.any(is_rob):
            # it is assumed that the faces dof are put before the cell dof
            is_rob = np.where(is_rob)[0]

            data = np.zeros(self.ndof(g))
            data[is_rob] = 1./(bc.robin_weight[is_rob]*g.face_areas[is_rob])
            M += sps.dia_matrix((data, 0), shape=(data.size, data.size))

        if bc_weight:
            return M, norm
        return M

    # ------------------------------------------------------------------------------#

    def rhs(self, g, data, bc_weight=1):
        """
        Return the righ-hand side for a discretization of a second order elliptic
        equation using RT0-P0 method. See self.matrix_rhs for a detaild
        description.

        Additional parameter:
        --------------------
        bc_weight: to use the infinity norm of the matrix to impose the
            boundary conditions. Default 1.

        """
        # Allow short variable names in backend function
        # pylint: disable=invalid-name

        param = data["param"]
        f = param.get_source(self)

        if g.dim == 0:
            return np.hstack(([0], f))

        bc = param.get_bc(self)
        bc_val = param.get_bc_val(self)

        assert not bool(bc is None) != bool(bc_val is None)

        rhs = np.zeros(self.ndof(g))
        if bc is None:
            return rhs

        # For dual discretizations, internal boundaries
        # are handled by assigning Dirichlet conditions. Thus, we remove them
        # from the is_neu (where they belong by default). As the dirichlet
        # values are simply added to the rhs, and the internal Dirichlet
        # conditions on the fractures SHOULD be homogeneous, we exclude them
        # from the dirichlet condition as well.
        is_neu = np.logical_and(bc.is_neu, np.logical_not(bc.is_internal))
        is_dir = np.logical_and(bc.is_dir, np.logical_not(bc.is_internal))
        is_rob = np.logical_and(bc.is_rob, np.logical_not(bc.is_internal))

        faces, _, sign = sps.find(g.cell_faces)
        sign = sign[np.unique(faces, return_index=True)[1]]

        if np.any(is_dir):
            is_dir = np.where(is_dir)[0]
            rhs[is_dir] += -sign[is_dir] * bc_val[is_dir]

        if np.any(is_rob):
            is_rob = np.where(is_rob)[0]
            rhs[is_rob] += -sign[is_rob] * bc_val[is_rob] / bc.robin_weight[is_rob]

        if np.any(is_neu):
            is_neu = np.where(is_neu)[0]
            rhs[is_neu] = sign[is_neu] * bc_weight * bc_val[is_neu]

        return rhs

    # ------------------------------------------------------------------------------#

    def extract_u(self, g, up):
        """  Extract the velocity from a RT0-P0 solution.

        Parameters
        ----------
        g : grid, or a subclass, with geometry fields computed.
        up : array (g.num_faces+g.num_cells)
            Solution, stored as [velocity,pressure]

        Return
        ------
        u : array (g.num_faces)
            Velocity at each face.

        """
        # pylint: disable=invalid-name
        return up[: g.num_faces]

    # ------------------------------------------------------------------------------#

    def extract_p(self, g, up):
        """  Extract the pressure from a RT0-P0 solution.

        Parameters
        ----------
        g : grid, or a subclass, with geometry fields computed.
        up : array (g.num_faces+g.num_cells)
            Solution, stored as [velocity,pressure]

        Return
        ------
        p : array (g.num_cells)
            Pressure at each cell.

        """
        # pylint: disable=invalid-name
        return up[g.num_faces :]

    # ------------------------------------------------------------------------------#

    def project_u(self, g, u, data):
        """  Project the velocity computed with a rt0 solver to obtain a
        piecewise constant vector field, one triplet for each cell.

        Parameters
        ----------
        g : grid, or a subclass, with geometry fields computed.
        u : array (g.num_faces) Velocity at each face.

        Return
        ------
        P0u : ndarray (3, g.num_faces) Velocity at each cell.

        """
        # Allow short variable names in backend function
        # pylint: disable=invalid-name

        if g.dim == 0:
            return np.zeros(3).reshape((3, 1))

        param = data["param"]
        a = param.get_aperture()

        faces, cells, sign = sps.find(g.cell_faces)
        index = np.argsort(cells)
        faces, sign = faces[index], sign[index]

        c_centers, f_normals, f_centers, R, dim, node_coords = pp.cg.map_grid(g)

        nodes, _, _ = sps.find(g.face_nodes)

        P0u = np.zeros((3, g.num_cells))

        for c in np.arange(g.num_cells):
            loc = slice(g.cell_faces.indptr[c], g.cell_faces.indptr[c + 1])
            faces_loc = faces[loc]

            # find the opposite node id for each face
            node = self.opposite_side_node(g.face_nodes, nodes, faces_loc)

            # extract the coordinates
            center = np.tile(c_centers[:, c], (node.size, 1)).T
            delta_c = center - node_coords[:, node]
            delta_f = f_centers[:, faces_loc] - node_coords[:, node]
            normals = f_normals[:, faces_loc]

            Pi = delta_c / np.einsum("ij,ij->j", delta_f, normals)

            # extract the velocity for the current cell
            P0u[dim, c] = np.dot(Pi, u[faces_loc]) * a[c]
            P0u[:, c] = np.dot(R.T, P0u[:, c])

        return P0u

    # ------------------------------------------------------------------------------#

    def massHdiv(self, K, c_volume, coord, sign, dim, HB):
        """ Compute the local mass Hdiv matrix using the mixed vem approach.

        Parameters
        ----------
        K : ndarray (g.dim, g.dim)
            Permeability of the cell.
        c_volume : scalar
            Cell volume.
        sign : array (num_faces_of_cell)
            +1 or -1 if the normal is inward or outward to the cell.

        Return
        ------
        out: ndarray (num_faces_of_cell, num_faces_of_cell)
            Local mass Hdiv matrix.
        """
        # Allow short variable names in this function
        # pylint: disable=invalid-name
        inv_K = np.linalg.inv(K)
        inv_K = linalg.block_diag(*([inv_K] * (dim + 1))) / c_volume

        coord = coord[0:dim, :]
        N = coord.flatten("F").reshape((-1, 1)) * np.ones((1, dim + 1)) - np.tile(
            coord, (dim + 1, 1)
        )

        C = np.diagflat(sign)

        return np.dot(C.T, np.dot(N.T, np.dot(HB, np.dot(inv_K, np.dot(N, C)))))

    # ------------------------------------------------------------------------------#

    @staticmethod
    def opposite_side_node(face_nodes, nodes, faces_loc):
        """
        Given a face return the node on the opposite side, typical request of a Raviart-Thomas
        approximation. This function is mainly for internal use.

        Parameters:
        ----------
        face_nodes: global map which contains, for each face, the node ids
        nodes: all the nodes in the grid after a find to the face_nodes map
        faces_loc: face ids for the current cel

        Return:
        -------
        opposite_node: for each face in faces_loc the id of the node at their opposite
            side

        """
        indptr = face_nodes.indptr
        face_nodes = [nodes[indptr[f] : indptr[f + 1]] for f in faces_loc]

        nodes_loc = np.unique(face_nodes)
        opposite_node = [
            np.setdiff1d(nodes_loc, f, assume_unique=True) for f in face_nodes
        ]
        return np.array(opposite_node).flatten()


# ------------------------------------------------------------------------------#
