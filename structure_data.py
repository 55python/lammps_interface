#!/usr/bin/env python
from datetime import date
import numpy as np
from scipy.spatial import distance
import math
import shlex
from CIFIO import CIF
from copy import copy
import itertools

try:
    import networkx as nx
except ImportError:
    print("Warning: could not load networkx module, this is needed to produce the lammps data file.")
    sys.exit()
from atomic import MASS, ATOMIC_NUMBER, COVALENT_RADII
from ccdc import CCDC_BOND_ORDERS
DEG2RAD=np.pi/180.

class MolecularGraph(nx.Graph):
    """Class to contain all information relating a structure file
    to a fully described classical system.
    Important specific arguments for atomic nodes:
    - mass
    - force_field_type
    - charge
    - cartesian_coordinates
    - description {contains all information about electronic environment
                   to make a decision on the final force_field_type}
        -hybridization [sp3, sp2, sp, aromatic]
    
    Important arguments for bond edges:
    - weight = 1
    - length
    - image_flag
    - force_field_type
    """

    def __init__(self, **kwargs):
        nx.Graph.__init__(self, **kwargs)
        # coordinates and distances will be kept in a matrix because 
        # networkx edge and node lookup is slow.
        self.coordinates = None
        self.distmatrix = None 

    def add_atomic_node(self, **kwargs):
        """Insert nodes into the graph from the cif file"""
        #update keywords with more atom info
        kwargs.update({'mass':MASS[kwargs['_atom_site_type_symbol']]})
        try:
            kwargs['charge'] = float(kwargs['_atom_type_partial_charge'])
        except KeyError:
            kwargs['charge'] = 0.0 

        kwargs.update({'index':self.number_of_nodes() + 1})
        #TODO(pboyd) should have some error checking here..
        n = kwargs.pop('_atom_site_label')
        self.add_node(n, **kwargs)
   
    def compute_bonding(self, scale_factor = 0.9):
        """Computes bonds between atoms based on covalent radii."""
        if (self.number_of_edges() > 0):
            # bonding found in cif file
            return
        for n1, n2 in itertools.combinations(self.nodes(), 2):
            node1, node2 = self.node[n1], self.node[n2]
            e1, e2 = node1['_atom_site_type_symbol'],\
                    node2['_atom_site_type_symbol']
            i1,i2 = node1['index']-1, node2['index']-1
            rad = (COVALENT_RADII[e1] + COVALENT_RADII[e2])
            if self.distance_matrix[i1,i2]*scale_factor < rad:
                self.add_edge(n1, n2, key=self.number_of_edges() + 1, order='S')
            # add image flag if necessary (and missing..)
    
    #TODO(pboyd) update this
    def compute_bond_image_flag(self):
        """Update bonds to contain bond type, distances, and min img
        shift."""
        supercells = np.array(list(itertools.product((-1, 0, 1), repeat=3)))
        unit_repr = np.array([5,5,5], dtype=int)
        for bond in self.bonds:
            atom1,atom2 = bond.atoms
            fcoords = atom2.scaled_pos(self.cell.inverse) + supercells
            coords = []
            for j in fcoords:
                coords.append(np.dot(j, self.cell.cell))
            coords = np.array(coords)
            dists = distance.cdist([atom1.coordinates[:3]], coords)
            dists = dists[0].tolist()
            image = dists.index(min(dists))
            dist = min(dists)
            sym = '.' if all([i==0 for i in supercells[image]]) else \
                    "1_%i%i%i"%(tuple(np.array(supercells[image],dtype=int) +
                                      unit_repr))
            bond.symflag = sym

    def add_bond_edge(self, **kwargs):
        """Add bond edges (weight factor = 1)"""
        #TODO(pboyd) should figure out if there are other cif keywords to identify
        # atom types
        n1 = kwargs.pop('_geom_bond_atom_site_label_1')
        n2 = kwargs.pop('_geom_bond_atom_site_label_2')
        kwargs.update({'weight': 1})
        self.add_edge(n1, n2, key=self.number_of_edges()+1, **kwargs)

    def compute_cartesian_coordinates(self, cell):
        """Compute the cartesian coordinates for each atom node"""
        coord_keys = ['_atom_site_x', '_atom_site_y', '_atom_site_z']
        fcoord_keys = ['_atom_site_fract_x', '_atom_site_fract_y', '_atom_site_fract_z']
        self.coordinates = np.empty((self.number_of_nodes(), 3))
        for node, data in self.nodes_iter(data=True):
            #TODO(pboyd) probably need more error checking..
            try:
                coordinates = np.array([float(data[i]) for i in coord_keys])
            except KeyError:
                coordinates = np.array([float(data[i]) for i in fcoord_keys])
                coordinates = np.dot(coordinates, cell.cell)
            data.update({'cartesian_coordinates':coordinates})

            self.coordinates[data['index']-1] = coordinates

    def compute_min_img_distances(self, cell):
        self.distance_matrix = np.empty((self.number_of_nodes(), self.number_of_nodes()))
        for n1, n2 in itertools.combinations(self.nodes(), 2):
            id1, id2 = self.node[n1]['index']-1,\
                                self.node[n2]['index']-1
            coords1, coords2 = self.coordinates[id1], self.coordinates[id2]
            dist = self.min_img_distance(coords1, coords2, cell)
            self.distance_matrix[id1][id2] = dist
            self.distance_matrix[id2][id1] = dist
    
    def min_img_distance(self, coords1, coords2, cell):
        one = np.dot(cell.inverse, coords1) % 1
        two = np.dot(cell.inverse, coords2) % 1
        three = np.around(one - two)
        four = np.dot(one - two - three, cell.cell)
        return np.linalg.norm(four)

    def compute_atom_typing(self):
        #TODO(pboyd) return if atoms already 'typed' in the .cif file
        # compute and store cycles
        cycles = []
        for node in self.atoms:
            label = atom.ciflabel
            for n in atom.neighbours:
                nlabel = self.atoms[n].ciflabel
                # fastest way I could think of..
                self.remove_edge(label, nlabel)
                cycle = []
                try:
                    cycle = list(nx.all_shortest_paths(self, label, nlabel))
                except nx.exception.NetworkXNoPath:
                    pass
                self.add_edge(label, nlabel)
                #FIXME MW edit to only store cycles < len(10)
                # should be a harmless edit but maybe need to test
                if(len(cycle) <= 10):
                    cycles += cycle

        for label, data in self.nodes_iter(data=True):
            # N O C S
            neighbours = self.neighbors(label)
            element = data['_atom_site_type_symbol']
            if element == "C":
                if len(neighbours) >= 4:
                    data.update({'hybridization':'sp3'})
                elif len(neighbours) == 3:
                    data.update({'hybridization':'sp2'})
                elif len(neighbours) <= 2:
                    data.update({'hybridization':'sp'})
            elif element == "N":
                if len(atom.neighbours) >= 3:
                    data.update({'hybridization':'sp3'})
                elif len(atom.neighbours) == 2:
                    data.update({'hybridization':'sp2'})
                elif len(atom.neighbours) == 1:
                    data.update({'hybridization':'sp'})
            elif element == "O":
                if len(atom.neighbours) >= 2:
                    data.update({'hybridization':'sp3'})
                elif len(atom.neighbours) == 1:
                    data.update({'hybridization':'sp2'})
            elif element == "S":
                if len(atom.neighbours) == 2:
                    data.update({'hybridization':'sp3'})
                elif len(atom.neighbours) == 1:
                    data.update({'hybridization':'sp2'})

        # convert to aromatic
        # probably not a good test for aromaticity..
        arom = set(["C", "N", "O", "S"])
        for cycle in cycles:
            elements = [self.nodes[k]['_atom_site_type_symbol'] for k in cycle]
            neigh = [self.neighbors[k] for k in cycle]
            if np.all(np.array(neigh) <= 3) and set(elements) <= arom:
                for a in cycle:
                    self.nodes[a]['hybridization'] = 'aromatic'
                    self.nodes[a]['cycle'] = True
                    self.nodes[a].setdefault('rings', []).append(cycle)

    #TODO(pboyd) update this
    def compute_bond_typing(self):
        #TODO(pboyd) return if bonds already 'typed' in the .cif file
        organic = set(["H", "C", "N", "O", "S"])
        for bond in self.bonds:
            atoms = bond.atoms
            elements = [a.element for a in atoms]
            samering = False
            if atoms[0].hybridization == "aromatic" and atoms[1].hybridization == "aromatic":
                for r in atoms[0].rings:
                    if atoms[1].ciflabel in r:
                        samering = True
                if(samering):
                    bond.order = 1.5

            if set(elements) == set(["C", "O"]):
                car = atoms[elements.index("C")]
                oxy = atoms[elements.index("O")]
                carnn = [self.atoms[j] for j in car.neighbours if j != oxy.index]
                try:
                    carnelem = [j.element for j in carnn]
                except:
                    carnelem = []

                oxynn = [self.atoms[j] for j in oxy.neighbours if j != car.index]
                try:
                    oxynelem = [j.element for j in oxynn]
                except:
                    oxynelem = []
                if "O" in carnelem:
                    at = carnn[carnelem.index("O")]
                    if len(at.neighbours) == 1:
                        if len(oxy.neighbours) == 1:
                            #CO2
                            car.hybridization = 'sp'
                            oxy.hybridization = 'sp2'
                            bond.order = 2.
                        else:
                            # ester
                            if set(oxynelem) <= organic:
                                car.hybridization = 'sp2'
                                oxy.hybridization = 'sp2'
                                bond.order = 1 # this is the ether part of an ester... 
                            #carboxylate?
                            else:
                                car.hybridization = 'aromatic'
                                oxy.hybridization = 'aromatic'
                                bond.order = 1.5

                    else:
                        atnelem = [self.atoms[k].element for k in at.neighbours]
                        if (set(atnelem) <= organic):
                            # ester
                            if len(oxy.neighbours) == 1:
                                car.hybridization = 'sp2'
                                oxy.hybridization = 'sp2'
                                bond.order = 2. # carbonyl part of ester
                            # some kind of resonance structure?
                            else:
                                car.hybridization = 'aromatic'
                                oxy.hybridization = 'aromatic'
                                bond.order = 1.5
                        else:
                            car.hybridization = 'aromatic'
                            oxy.hybridization = 'aromatic'
                            bond.order = 1.5
                if "N" in carnelem:
                    at = carnn[carnelem.index("N")]
                    # C=O of amide group
                    if len(oxy.neighbours) == 1:
                        bond.order = 1.5
                        car.hybridization = 'aromatic'
                        oxy.hybridization = 'aromatic'
                # only one carbon oxygen connection.. could be C=O, R-C-O-R, R-C=O-R
                if (not "O" in carnelem) and (not "N" in carnelem):
                    if len(oxynn) > 0:
                        # ether
                        oxy.hybridization = 'sp3'
                        bond.order = 1.0
                    else:
                        if car.is_cycle and car.hybridization == 'aromatic':
                            oxy.hybridization = 'aromatic'
                            bond.order = 1.5
                        # carbonyl
                        else:
                            oxy.hybridization = 'sp2'
                            bond.order = 2.0
            if set(elements) == set(["C", "N"]) and not samering:
                car = atoms[elements.index("C")]
                nit = atoms[elements.index("N")]
                nitnn = [self.atoms[j] for j in nit.neighbours if j != car.index]
                nitnelem = [k.element for k in nitnn]
                # aromatic amine connected -- assume part of delocalized system
                if car.hybridization == 'aromatic' and set(['H']) == set(nitnelem):
                    bond.order = 1.5
                    nit.hybridization = 'aromatic'
                # amide?
                elif len(car.neighbours) == 3 and len(nitnn) >=2:
                    if "O" in carnelem:
                        bond.order = 1.5 # (amide)
                        nit.hybridization = 'aromatic'
            if (not atoms[0].is_cycle) and (not atoms[1].is_cycle) and (set(elements) <= organic):
                if set([a.hybridization for a in atoms]) == set(['sp2']):
                    # check bond length.. probably not a good indicator..
                    try:
                        cr1 = COVALENT_RADII['%s_2'%atoms[0].element]
                    except KeyError:
                        cr1 = COVALENT_RADII[atoms[0].element]
                    try:
                        cr2 = COVALENT_RADII['%s_2'%(atoms[1].element)]
                    except KeyError:
                        cr2 = COVALENT_RADII[atoms[1].element]
                    covrad = cr1 + cr2
                    if (bond.length <= covrad*.95):
                        bond.order = 2.0
                elif set([a.hybridization for a in atoms]) == set(['sp']):
                    try:
                        cr1 = COVALENT_RADII['%s_1'%atoms[0].element]
                    except KeyError:
                        cr1 = COVALENT_RADII[atoms[0].element]
                    try:
                        cr2 = COVALENT_RADII['%s_1'%(atoms[1].element)]
                    except KeyError:
                        cr2 = COVALENT_RADII[atoms[1].element]
                    covrad = cr1 + cr2 
                    if (bond.length <= covrad*.95):
                        bond.order = 3.0
    def atomic_node_sanity_check(self):
        """Check for specific keyword/value pairs. Exit if non-existent"""

    def show(self):
        nx.draw(self)

def from_CIF(cifname):
    """Reads the structure data from the CIF
    - currently does not read the symmetry of the cell
    - does not unpack the assymetric unit (assumes P1)
    - assumes that the appropriate keys are in the cifobj (no error checking)
    """

    cifobj = CIF()
    cifobj.read(cifname)

    data = cifobj._data
    # obtain atoms and cell
    cell = Cell()
    # add data to molecular graph (to be parsed later..)
    mg = MolecularGraph()
    cellparams = [float(i) for i in [data['_cell_length_a'], 
                                     data['_cell_length_b'], 
                                     data['_cell_length_c'],
                                     data['_cell_angle_alpha'], 
                                     data['_cell_angle_beta'], 
                                     data['_cell_angle_gamma']]]
    cell.set_params(cellparams)
    
    #add atom nodes
    id = cifobj.block_order.index('atoms')
    atheads = cifobj._headings[id]
    for atom_data in zip(*[data[i] for i in atheads]):
        kwargs = {a:j.strip() for a, j in zip(atheads, atom_data)}
        mg.add_atomic_node(**kwargs)

    # add bond edges, if they exist
    try:
        id = cifobj.block_order.index('bonds')
        bondheads = cifobj._headings[id]
        for bond_data in zip(*[data[i] for i in bondheads]):
            kwargs = {a:j.strip() for a, j in zip(bondheads, bond_data)}
            mg.add_bond_edge(**kwargs)
    except:
        # catch no bonds
        print("No bonds reported in cif file - computing bonding..")
    mg.compute_cartesian_coordinates(cell)
    mg.compute_min_img_distances(cell)
    mg.compute_bonding()
    return cell, mg
    #x, y, z = [], [], []
    #if '_atom_site_fract_x' in data:
    #    fx = np.array([float(j) for j in data['_atom_site_fract_x']])
    #elif (('_atom_site_x' in data) or 
    #      ('_atom_site_cartn_x' in data) or 
    #      ('_atom_site_Cartn_x' in data)):
    #    try:
    #        x = np.array([float(j) for j in data['_atom_site_x']])
    #    except Keyerror:
    #        pass
    #    try:
    #        x = np.array([float(j) for j in data['_atom_site_cartn_x']])
    #    except KeyError:
    #        pass
    #    try:
    #        x = np.array([float(j) for j in data['_atom_site_Cartn_x']])
    #    except KeyError:
    #        pass
    #
    #if '_atom_site_fract_y' in data:
    #    fy = np.array([float(j) for j in data['_atom_site_fract_y']])
    #elif (('_atom_site_y' in data) or 
    #      ('_atom_site_cartn_y' in data) or 
    #      ('_atom_site_Cartn_y' in data)):
    #    try:
    #        y = np.array([float(j) for j in data['_atom_site_y']])
    #    except Keyerror:
    #        pass
    #    try:
    #        y = np.array([float(j) for j in data['_atom_site_cartn_y']])
    #    except KeyError:
    #        pass
    #    try:
    #        y = np.array([float(j) for j in data['_atom_site_Cartn_y']])
    #    except KeyError:
    #        pass

    #if '_atom_site_fract_z' in data:
    #    fz = np.array([float(j) for j in data['_atom_site_fract_z']])
    #elif (('_atom_site_z' in data) or 
    #      ('_atom_site_cartn_z' in data) or 
    #      ('_atom_site_Cartn_z' in data)):
    #    try:
    #        z = np.array([float(j) for j in data['_atom_site_z']])
    #    except Keyerror:
    #        pass
    #    try:
    #        z = np.array([float(j) for j in data['_atom_site_cartn_z']])
    #    except KeyError:
    #        pass
    #    try:
    #        z = np.array([float(j) for j in data['_atom_site_Cartn_z']])
    #    except KeyError:
    #        pass
    #try:
    #    for xx, yy, zz in zip(fx, fy, fz):
    #        cx,cy,cz = np.dot(np.array([xx,yy,zz]), cell.cell)
    #        x.append(cx)
    #        y.append(cy)
    #        z.append(cz)
    #    x = np.array(x)
    #    y = np.array(y)
    #    z = np.array(z)
    #except:
    #    pass
    ## Charge assignment may have to be a bit more inclusive than just setting _atom_site_charge
    ## in the .cif file.. will have to think of a user-friendly way to introduce charges..
    #if '_atom_type_partial_charge' in data:
    #    charges = [float(j.strip()) for j in data['_atom_type_partial_charge']]
    #else:

    #    charges = [0. for i in range(0, len(x))]

    #charge = np.sum(charges)

    ## bunch of try excepts for different important labels in the cif file.
    #try:
    #    label = data['_atom_site_label']
    #except KeyError:
    #    label = ['X%i'%(i) for i in range(0, len(x))]
    #    print("Warning, no atom labels specified in cif file")

    #try:
    #    element = data['_atom_site_type_symbol']
    #except KeyError:
    #    element = ['X' for i in range(0, len(x))]
    #    print("Warning, no elements specified in cif file. "+
    #            "This will make generating ff files very difficult!")
    #guess_atom_types = False
    #try:
    #    ff_param = [i.strip() for i in data['_atom_site_description']]
    #except:
    #    guess_atom_types = True
    #    ff_param = [None for i in range(0, len(x))]
    #    print("Warning, force field atom types not specified in the cif file."+
    #            " Attempting to guess atom types.")
    #
    #index = 0
    #for l,e,ff,fx,fy,fz,c in zip(label,element,ff_param,x,y,z,charges):
    #    atom = Atom(element=e.strip(), coordinates = np.array([fx,fy,fz]))
    #    atom.force_field_type = ff
    #    atom.ciflabel = l.strip()
    #    atom.charge = c 
    #    self.atoms.append(atom)
    ## obtain bonds
    #if '_geom_bond_atom_site_label_1' in data:
    #    a, b, type = (data['_geom_bond_atom_site_label_1'], 
    #                  data['_geom_bond_atom_site_label_2'], 
    #                  data['_ccdc_geom_bond_type'])

    #    try:
    #        symms = data['_geom_bond_site_symmetry_2']
    #    except KeyError:
    #        symms = ['.' for i in range(len(a))]

    #    try:
    #        dists = data['_geom_bond_distance']
    #    except KeyError:
    #        dists = [0.0 for i in range(len(a))]


    #    for (label1, label2, t, dist, sym)in zip(a,b,type,dists,symms):
    #        atm1 = self.get_atom_from_label(label1.strip())
    #        atm2 = self.get_atom_from_label(label2.strip())
    #        #TODO(check if atm2 crosses a periodic boundary to bond with atm1)
    #        #.cif file double counts bonds for some reason.. maybe symmetry related
    #        try:
    #            if (atm2.index not in atm1.neighbours) and (atm1.index not in atm2.neighbours):
    #                atm1.neighbours.append(atm2.index)
    #                atm2.neighbours.append(atm1.index)
    #                bond = Bond(atm1=atm1, atm2=atm2, 
    #                            order=CCDC_BOND_ORDERS[t.strip()])
    #                bond.length = float(dist)
    #                bond.symflag = sym.strip()
    #                self.bonds.append(bond)
    #        except AttributeError:
    #            print("Warning, bonding seems to be misspecified in .cif file")

    #    if '_geom_bond_site_symmetry_2' not in data.keys():
    #        self.compute_bond_image_flag()
    #    self.obtain_graph()
    #    #TODO unwrap symmetry elements if they exist
    #else:
    #    print("No bonding found in file, attempting to populate bonding..")
    #    self.compute_bonding()
    #    self.obtain_graph()
    #self.compute_atom_bond_typing()
class Structure(object):

    def __init__(self, name):
        self.name = name
        self.cell = Cell()
        self.atoms = []
        self.bonds = []
        self.angles = []
        self.dihedrals = []
        self.impropers = []
        self.pairs = []
        self.charge = 0.0
        try:
            self.graph = nx.Graph()
        except NameError:
            self.graph = None

    

    def obtain_graph(self):
        """Attempt to assign bond and atom types based on graph analysis."""
        if self.graph is None:
            print("Warning atom and bond typing could not be completed due "+
                    "to lacking networkx module. All bonds will be of 'single' type" +
                    " which may result in a poor description of your system!")
            return

        for atom in self.atoms:
            self.graph.add_node(atom.ciflabel)
        for bond in self.bonds:
            at1, at2 = bond.atoms
            self.graph.add_edge(at1.ciflabel, at2.ciflabel)

    def compute_bonding(self, scale_factor=0.9):
        coords = np.array([a.coordinates for a in self.atoms])
        elems = [a.element for a in self.atoms]
        distmat = np.empty((coords.shape[0], coords.shape[0]))
        organics = set(["H", "C", "N", "O", "F", "Cl", "S", "B"])
        for (i,j) in zip(*np.triu_indices(coords.shape[0], k=1)):
            e1 = elems[i]
            e2 = elems[j]
            dist = self.min_img_distance(coords[i], coords[j])
            distmat[j,i] = dist
            covrad = COVALENT_RADII[e1] + COVALENT_RADII[e2]
            if(dist*scale_factor < covrad):
                # make sure hydrogens don't bond to metals (specific case..)
                if "H" in [e1, e2] and not set([e1, e2])<=organics:
                    pass
                else:
                    # figure out bond orders when typing.
                    bond = Bond(atm1=self.atoms[i], atm2=self.atoms[j], order=1)
                    bond.length = dist
                    self.bonds.append(bond)
                    self.atoms[i].neighbours.append(self.atoms[j].index)
                    self.atoms[j].neighbours.append(self.atoms[i].index)
        # make sure hydrogens don't bond to other hydrogens.. umm. except for H2
        delbonds = []
        for idx, bond in enumerate(self.bonds):
            a1, a2 = bond.atoms
            if a1.element == "H" and a2.element == "H":
                if len(a1.neighbours) > 1 or len(a2.neighbours) > 1:
                    delbonds.append(idx)
                    del(a1.neighbours[a1.neighbours.index(a2.index)])
                    del(a2.neighbours[a2.neighbours.index(a1.index)])
        for j in reversed(sorted(delbonds)):
            del(self.bonds[j])
        # re-index bonds after modification
        for idx, bond in enumerate(self.bonds):
            bond.index = idx
        self.compute_bond_image_flag()

    def get_atom_from_label(self, label):
        for atom in self.atoms:
            if atom.ciflabel == label:
                return atom

    def compute_angles(self):
        for atom in self.atoms:
            if len(atom.neighbours) < 2:
                continue
            angles = itertools.combinations(atom.neighbours, 2)
            for (lid, rid) in angles:
                left_atom = self.atoms[lid]
                right_atom = self.atoms[rid]
                abbond = self.get_bond(left_atom, atom)
                bcbond = self.get_bond(atom, right_atom)
                angle = Angle(abbond, bcbond)
                self.angles.append(angle)

    def get_bond(self, atom1, atom2):
        for bond in self.bonds:
            if set((atom1, atom2)) ==  set(bond.atoms):
                return bond
        return None

    def compute_dihedrals(self):
        done_bs=[]
        for atom_b in self.atoms:
            ib = atom_b.index
            ib_type = atom_b.ff_type_index
            angles = itertools.permutations(atom_b.neighbours, 2)
            done_bs.append(atom_b)
            for ia, ic in angles:
                atom_a = self.atoms[ia]
                atom_c = self.atoms[ic]
                ia_type = atom_a.ff_type_index
                ic_type = atom_c.ff_type_index
                # ignore cases where the c atom has already
                # been used as a b atom. Otherwise this will double-count
                # dihedral angles in the reverse order..
                if atom_c in done_bs:
                    continue
                c_neighbours = [i for i in atom_c.neighbours if ib != i and
                                ia != i]
                for id in c_neighbours:
                    atom_d = self.atoms[id]
                    id_type = atom_d.ff_type_index
                    
                    angle1 = self.get_angle(atom_a, atom_b, atom_c)
                    angle2 = self.get_angle(atom_b, atom_c, atom_d)
                    if angle1 is None:
                        angle1 = self.get_angle(atom_c, atom_b, atom_a)
                    if angle2 is None:
                        angle2 = self.get_angle(atom_d, atom_c, atom_b)
                    dihedral = Dihedral(angle1, angle2)
                    self.dihedrals.append(dihedral)
    
    def get_angle(self, atom_a, atom_b, atom_c):
        for angle in self.angles:
            if (atom_a, atom_b, atom_c) ==  angle.atoms:
                return angle
        return None

    def compute_improper_dihedrals(self):
        count = 0
        improper_type = {}

        for atom_b in self.atoms:
            if len(atom_b.neighbours) != 3:
                continue
            ib = atom_b.index
            # three improper torsion angles about each atom
            local_impropers = list(itertools.permutations(atom_b.neighbours))
            for idx in range(0, 6, 2):
                (ia, ic, id) = local_impropers[idx]
                atom_a, atom_c, atom_d = self.atoms[ia], self.atoms[ic], self.atoms[id]

                abbond = self.get_bond(atom_a, atom_b)
                bcbond = self.get_bond(atom_b, atom_c)
                bdbond = self.get_bond(atom_b, atom_d)
                improper = ImproperDihedral(abbond, bcbond, bdbond)
                self.impropers.append(improper)
    
    def compute_pair_terms(self):
        """Place holder for hydrogen bonding?"""
        for i in self.atoms:
            pair = PairTerm(i, i)
            self.pairs.append(pair)

    def minimum_cell(self, cutoff=12.5):
        """Determine the minimum cell size such that half the orthogonal cell
        width is greater than or equal to 'cutoff' which is default
        12.5 angstroms.
        
        NB: this replaces and overwrites the original unit cell data 
            with a supercell. There may be a better way to do this 
            if one needs to keep both the super- and unit cells.
        """
        sc = self.cell.minimum_supercell(cutoff)
        unitatomlen = len(self.atoms)
        unit_repr = np.array([5,5,5], dtype=int)
        origincell = np.array([0., 0., 0.])
        if np.any(np.array(sc) > 1):
            print("Warning: unit cell is not large enough to"
            +" support a non-bonded cutoff of %.2f Angstroms\n"%cutoff +
            "Re-sizing to a %i x %i x %i supercell. "%(sc))
            cells = list(itertools.product(*[itertools.product(range(j)) for j in sc]))
            repatoms = []
            repbonds = []
            maxcell = np.array(sc)
            for cell in cells[1:]:
                repatoms += self.replicate(cell)
            self.atoms += repatoms
            totatomlen = len(self.atoms)
            # do bonding
            delbonds = []
            for cell in cells:
                newcell = np.array(cell).flatten()
                offset = cells.index(cell)*unitatomlen
                for bidx, bond in enumerate(self.bonds):
                    (atom1, atom2) = bond.atoms
                    newatom1 = self.atoms[(atom1.index%unitatomlen + offset)]
                    newatom2 = self.atoms[(atom2.index%unitatomlen + offset)]
                    if bond.symflag != '.':
                        # check if the current cell is at the extrema of the supercell
                        ocell = newcell + np.array([int(j) for j in bond.symflag[2:]]) - unit_repr
                        imgcell = ocell % maxcell
                        imgoffset = cells.index(tuple([tuple([i]) for i in imgcell]))*unitatomlen
                        newatom2 = self.atoms[atom2.index%unitatomlen + imgoffset]
                        # new symflag
                        if(np.all(newcell == np.zeros(3))):
                            delbonds.append(bidx)
                            if self.graph is not None:
                                self.graph.remove_edge(atom1.ciflabel, atom2.ciflabel)

                        newbond = Bond(newatom1, newatom2, order=bond.order)
                        newbond.length = bond.length
                        if any(ocell < origincell) or any(ocell >= maxcell):
                            newflaga = np.array([5,5,5])
                            newflaga[np.where(ocell >= maxcell)] = 6
                            newflaga[np.where(ocell < np.zeros(3))] = 4
                            newflag = "1_%i%i%i"%(tuple(newflaga))
                            newbond.symflag = newflag
                        else:
                            newbond.symflag = '.'
                        repbonds.append(newbond)
                        oldind2 = atom2.index + offset
                        try:
                            del(newatom1.neighbours[newatom1.neighbours.index(oldind2)])
                        except ValueError:
                            pass
                        newatom1.neighbours.append(newatom2.index)
                        oldind1 = atom1.index + imgoffset
                        try:
                            del(newatom2.neighbours[newatom2.neighbours.index(oldind1)])
                        except ValueError:
                            pass
                        newatom2.neighbours.append(newatom1.index)
                    else:
                        if(np.any(newcell != np.zeros(3))):
                            newbond = Bond(newatom1, newatom2, order=bond.order)
                            newbond.length = bond.length
                            newbond.symflag = '.'
                            repbonds.append(newbond)
                            #new images, shouldn't have to delete existing neighbours
                            newatom1.neighbours.append(newatom2.index)
                            newatom2.neighbours.append(newatom1.index)
                    if self.graph is not None:
                        if newatom1.ciflabel not in self.graph.nodes():
                            self.graph.add_node(newatom1.ciflabel)
                        if newatom2.ciflabel not in self.graph.nodes():
                            self.graph.add_node(newatom2.ciflabel)
                        self.graph.add_edge(newatom1.ciflabel, newatom2.ciflabel)

            self.bonds += repbonds
            # update lattice boxsize to supercell
            self.cell.multiply(sc)
            for idbad in reversed(sorted(delbonds)):
                del(self.bonds[idbad])
            # re-index bonds
            for newidx, bond in enumerate(self.bonds):
                bond.index=newidx

        # re-calculate bonding across periodic images. 
    def replicate(self, image):
        """Replicate the structure in the image direction"""
        trans = np.sum(np.multiply(self.cell.cell, np.array(image)).T, axis=1)
        l = len(self.atoms)
        repatoms = []
        for atom in self.atoms:
            newatom = copy(atom)
            newatom.coordinates += trans
            repatoms.append(newatom)
        return repatoms

    def min_img_distance(self, coords1, coords2):
        one = np.dot(self.cell.inverse, coords1) % 1
        two = np.dot(self.cell.inverse, coords2) % 1
        three = np.around(one - two)
        four = np.dot(one - two - three, self.cell.cell)
        return np.linalg.norm(four)


    def write_cif(self):
        """Currently used for debugging purposes"""
        c = CIF(name="%s.debug"%self.name)
        # data block
        c.add_data("data", data_=self.name)
        c.add_data("data", _audit_creation_date=
                            CIF.label(c.get_time()))
        c.add_data("data", _audit_creation_method=
                            CIF.label("Lammps Interface v.%s"%(str(0))))
        if self.charge:
            c.add_data("data", _chemical_properties_physical=
                               "net charge is %12.5f"%(self.charge))

        # sym block
        c.add_data("sym", _symmetry_space_group_name_H_M=
                            CIF.label("P1"))
        c.add_data("sym", _symmetry_Int_Tables_number=
                            CIF.label("1"))
        c.add_data("sym", _symmetry_cell_setting=
                            CIF.label("triclinic"))

        # sym loop block
        c.add_data("sym_loop", _symmetry_equiv_pos_as_xyz=
                            CIF.label("'x, y, z'"))

        # cell block
        c.add_data("cell", _cell_length_a=CIF.cell_length_a(self.cell.a))
        c.add_data("cell", _cell_length_b=CIF.cell_length_b(self.cell.b))
        c.add_data("cell", _cell_length_c=CIF.cell_length_c(self.cell.c))
        c.add_data("cell", _cell_angle_alpha=CIF.cell_angle_alpha(self.cell.alpha))
        c.add_data("cell", _cell_angle_beta=CIF.cell_angle_beta(self.cell.beta))
        c.add_data("cell", _cell_angle_gamma=CIF.cell_angle_gamma(self.cell.gamma))
        # atom block
        element_counter = {}
        for id, atom in enumerate(self.atoms):
            label = "%s%i"%(atom.element, atom.index)
            c.add_data("atoms", _atom_site_label=
                                    CIF.atom_site_label(label))
            c.add_data("atoms", _atom_site_type_symbol=
                                    CIF.atom_site_type_symbol(atom.element))
            c.add_data("atoms", _atom_site_description=
                                    CIF.atom_site_description(atom.force_field_type))
            fc = atom.scaled_pos(self.cell.inverse)
            c.add_data("atoms", _atom_site_fract_x=
                                    CIF.atom_site_fract_x(fc[0]))
            c.add_data("atoms", _atom_site_fract_y=
                                    CIF.atom_site_fract_y(fc[1]))
            c.add_data("atoms", _atom_site_fract_z=
                                    CIF.atom_site_fract_z(fc[2]))

        # bond block
        # must re-sort them based on bond type (Mat Sudio)
        tosort = [(bond.order, bond) for bond in self.bonds]
        for ord, bond in sorted(tosort, key=lambda tup: tup[0]):
            at1, at2 = bond.atoms
            type = CCDC_BOND_ORDERS[bond.order]
            dist = bond.length
            sym = bond.symflag

            label1 = "%s%i"%(at1.element, at1.index) 
            label2 = "%s%i"%(at2.element, at2.index)
            c.add_data("bonds", _geom_bond_atom_site_label_1=
                                        CIF.geom_bond_atom_site_label_1(label1))
            c.add_data("bonds", _geom_bond_atom_site_label_2=
                                        CIF.geom_bond_atom_site_label_2(label2))
            c.add_data("bonds", _geom_bond_distance=
                                        CIF.geom_bond_distance(dist))
            c.add_data("bonds", _geom_bond_site_symmetry_2=
                                        CIF.geom_bond_site_symmetry_2(sym))
            c.add_data("bonds", _ccdc_geom_bond_type=
                                        CIF.ccdc_geom_bond_type(type))

        file = open("%s.cif"%c.name, "w")
        file.writelines(str(c))
        file.close()

class Bond(object):
    __ID = 0

    def __init__(self, atm1=None, atm2=None, order=1):
        self.index = self.__ID
        self.order = order
        self._atoms = (atm1, atm2)
        self.length = 0.
        self.symflag = 0
        self.ff_type_index = 0
        self.midpoint = np.array([0., 0., 0.])
        self.potential = None
        Bond.__ID += 1

    def compute_length(self, coord1, coord2):
        return np.linalg.norm(np.array(coord2) - np.array(coord1))

    def set_atoms(self, atm1, atm2):
        self._atoms = (atm1, atm2)

    def get_atoms(self):
        return self._atoms

    atoms = property(get_atoms, set_atoms)
    
    @property
    def indices(self):
        if not None in self.atoms:
            return (self.atoms[0].index, self.atoms[1].index)
        return (None, None)

    @property
    def elements(self):
        if not None in self.atoms:
            return (self.atoms[0].element, self.atoms[1].element)
        return (None, None)

class Angle(object):
    __ID = 0
    def __init__(self, abbond=None, bcbond=None):
        """Class to contain angles. Atoms are labelled according to the angle:
        a   c
         \ /
          b 
        """
        # atoms are obtained from the bonds.
        self._atoms = (None, None, None)
        if abbond is not None and bcbond is not None:
            self.bonds = (abbond, bcbond)
        else:
            self._bonds = (abbond, bcbond)
        self.ff_type_index = 0
        self.potential = None
        self._angle = 0.
        self.index = self.__ID
        Angle.__ID += 1

    def set_bonds(self, bonds):
        """order is assumed (ab_bond, bc_bond)"""
        self._bonds = bonds
        atm1, atm2 = bonds[0].atoms
        atm3, atm4 = bonds[1].atoms

        self._atoms = list(self._atoms)
        if atm1 in (atm3, atm4):
            self._atoms[0] = atm2
            self._atoms[1] = atm1
            if atm1 == atm3:
                self._atoms[2] = atm4
            else:
                self._atoms[2] = atm3

        elif atm2 in (atm3, atm4):
            self._atoms[0] = atm1
            self._atoms[1] = atm2
            if atm2 == atm3:
                self._atoms[2] = atm4
            else:
                self._atoms[2] = atm3
        self._atoms = tuple(self._atoms)

    def get_bonds(self):
        return self._bonds

    bonds = property(get_bonds, set_bonds)

    @property
    def ab_bond(self):
        return self._bonds[0]
   
    @property
    def bc_bond(self):
        return self._bonds[1]
   
    @property
    def atoms(self):
        return self._atoms

    @property
    def a_atom(self):
        return self._atoms[0]

    @property
    def b_atom(self):
        return self._atoms[1]

    @property
    def c_atom(self):
        return self._atoms[2]

class Dihedral(object):
    """Class to store dihedral angles
    a
     \ 
      b -- c
            \ 
             d

    """
    __ID = 0
    def __init__(self, angle1=None, angle2=None):
        self._atoms = (None, None, None, None)
        self._bonds = (None, None, None)
        # angles of the form: angle_abc, angle_bcd
        self._angles = (angle1, angle2)
        if not None in (angle1, angle2):
            self.angles = (angle1, angle2)
        self.ff_type_index = 0
        self.index = self.__ID
        self.potential = None
        Dihedral.__ID += 1

    def set_angles(self, angles):
        angle1, angle2 = angles
        bonds1 = angle1.bonds
        bonds2 = angle2.bonds

        if angle1.bc_bond != angle2.ab_bond:
            if angle1.bc_bond == angle2.bc_bond:
                angle2.bonds = tuple(reversed(bonds2))
            elif angle1.ab_bond == angle2.ab_bond:
                angle1.bonds = tuple(reversed(bonds1))
            elif angle1.ab_bond == angle2.bc_bond:
                angle1.bonds = tuple(reversed(bonds1))
                angle2.bonds = tuple(reversed(bonds2))
        self._angles = (angle1, angle2)

        assert angle1.bc_bond == angle2.ab_bond

        assert angle1.b_atom == angle2.a_atom

        assert angle1.c_atom == angle2.b_atom

        self._atoms = tuple([angle1.a_atom, angle1.b_atom,
                             angle2.b_atom, angle2.c_atom])
        self._bonds = tuple([angle1.ab_bond, angle1.bc_bond, angle2.bc_bond])

    def get_angles(self):
        return self._angles

    angles = property(get_angles, set_angles)

    @property
    def a_atom(self):
        return self._atoms[0]

    @property
    def b_atom(self):
        return self._atoms[1]

    @property
    def c_atom(self):
        return self._atoms[2]
    
    @property
    def d_atom(self):
        return self._atoms[3]
    
    @property
    def atoms(self):
        return self._atoms

    @property
    def ab_bond(self):
        return self._bonds[0]

    @property
    def bc_bond(self):
        return self._bonds[1]

    @property
    def cd_bond(self):
        return self._bonds[2]

    @property
    def bonds(self):
        return self._bonds

    @property
    def abc_angle(self):
        return self._angles[0]

    @property
    def bcd_angle(self):
        return self._angles[1]

class PairTerm(object):
    """Place holder for VDW and other
    non-bonded potentials.

    """
    __ID = 0

    def __init__(self, atm1=None, atm2=None):
        self.ff_type_index = 0
        self._atoms = (atm1, atm2)
        self.potential = None
        self.index = self.__ID
        PairTerm.__ID += 1
    
    def set_atoms(self, atm1, atm2):
        self._atoms = (atm1, atm2)

    def get_atoms(self):
        return self._atoms

    atoms = property(get_atoms, set_atoms)
    
    @property
    def indices(self):
        if not None in self.atoms:
            return (self.atoms[0].index, self.atoms[1].index)
        return (None, None)

    @property
    def elements(self):
        if not None in self.atoms:
            return (self.atoms[0].element, self.atoms[1].element)
        return (None, None)

class ImproperDihedral(object):
    """Class to store improper dihedral angles

    a
     \ 
      b -- c
      |
      d

    """
    __ID = 0
    def __init__(self, bond1=None, bond2=None, bond3=None):
        self._atoms = (None, None, None, None)
        self._bonds = (bond1, bond2, bond3)
        if not None in (bond1, bond2, bond3):
            self.bonds = (bond1, bond2, bond3)
        self.ff_type_index = 0
        self.potential = None
        self.index = self.__ID
        ImproperDihedral.__ID += 1
    
    def set_bonds(self, bonds):
        self._angles = bonds
        bond1, bond2, bond3 = bonds
        self._atoms = [None, None, None, None]
        for a1 in bond1.atoms:
            for a2 in bond2.atoms:
                for a3 in bond3.atoms:
                    if a1 == a2 == a3:
                        self._atoms[1] = a1

        ab1, ab2 = bond1.atoms
        ab3, ab4 = bond2.atoms
        ab5, ab6 = bond3.atoms

        if ab1 == self._atoms[1]:
            self._atoms[0] = ab2
        else:
            self._atoms[0] = ab1

        if ab3 == self._atoms[1]:
            self._atoms[2] = ab4
        else:
            self._atoms[2] = ab3

        if ab5 == self._atoms[1]:
            self._atoms[3] = ab6
        else:
            self._atoms[3] = ab5

    def get_bonds(self):
        return self._bonds

    bonds = property(get_bonds, set_bonds)

    @property
    def a_atom(self):
        return self._atoms[0]

    @property
    def b_atom(self):
        return self._atoms[1]

    @property
    def c_atom(self):
        return self._atoms[2]
    
    @property
    def d_atom(self):
        return self._atoms[3]
    
    @property
    def atoms(self):
        return self._atoms

    @property
    def ab_bond(self):
        return self._bonds[0]

    @property
    def bc_bond(self):
        return self._bonds[1]

    @property
    def bd_bond(self):
        return self._bonds[2]

class Atom(object):
    __ID = 0
    def __init__(self, element="X", coordinates=np.zeros(3)):
        self.element = element
        self.index = self.__ID
        self.neighbours = []
        self.ciflabel = None
        self.images = []
        self.rings = []
        self.molecule_id = (None, 0)
        self.is_cycle = False
        self.hybridization = ''
        self.force_field_type = None
        self.coordinates = coordinates
        self.charge = 0.
        self.ff_type_index = 0 # keeps track of the unique integer value assigned to the force field type
        Atom.__ID += 1
        self.image_index = -1 # If a copy, keeps the original index here.
        self.h_bond_donor = False # keep track of h-bonding atoms (for DREIDING)

    def scaled_pos(self, inv_cell):
        return np.dot(inv_cell, self.coordinates[:3])

    def in_cell_scaled(self, inv_cell):
        return np.array([i%1 for i in self.scaled_pos(inv_cell)])

    def in_cell(self, cell, inv_cell):
        return np.dot(self.in_cell_scaled(inv_cell), cell)

    @property
    def mass(self):
        return MASS[self.element]

    @property
    def atomic_number(self):
        return ATOMIC_NUMBER.index(self.element)

    def __copy__(self):
        a = Atom()
        a.element = self.element[:]
        # index determined automatically
        # neighbours re-calculated
        a.ciflabel = "%s%i"%(a.element, a.index)
        a.hybridization = self.hybridization[:]
        a.coordinates = self.coordinates.copy()
        a.charge = float(self.charge)
        a.ff_type_index = int(self.ff_type_index)
        a.force_field_type = self.force_field_type[:]
        a.image_index = self.index
        a.h_bond_donor = self.h_bond_donor
        
        return a

class Cell(object):
    def __init__(self):
        self._cell = np.identity(3, dtype=np.float64)
        # cell parameters (a, b, c, alpha, beta, gamma)
        self._params = (1., 1., 1., 90., 90., 90.)
        self._inverse = None

    @property
    def volume(self):
        """Calculate cell volume a.bxc."""
        b_cross_c = cross(self.cell[1], self.cell[2])
        return dot(self.cell[0], b_cross_c)

    def get_cell(self):
        """Get the 3x3 vector cell representation."""
        return self._cell

    def set_cell(self, value):
        """Set cell and params from the cell representation."""
        # Class internally expects an array
        self._cell = np.array(value).reshape((3,3))
        self.__mkparam()
        self.__mklammps()
        self._inverse = np.linalg.inv(self.cell.T)

    # Property so that params are updated when cell is set
    cell = property(get_cell, set_cell)

    def get_params(self):
        """Get the six parameter cell representation as a tuple."""
        return tuple(self._params)

    def set_params(self, value):
        """Set cell and params from the cell parameters."""
        self._params = value
        self.__mkcell()
        self.__mklammps()
        self._inverse = np.linalg.inv(self.cell.T)

    params = property(get_params, set_params)

    def minimum_supercell(self, cutoff):
        """Calculate the smallest supercell with a half-cell width cutoff."""
        a_cross_b = np.cross(self.cell[0], self.cell[1])
        b_cross_c = np.cross(self.cell[1], self.cell[2])
        c_cross_a = np.cross(self.cell[2], self.cell[0])

        volume = np.dot(self.cell[0], b_cross_c)

        widths = [volume / np.linalg.norm(b_cross_c),
                  volume / np.linalg.norm(c_cross_a),
                  volume / np.linalg.norm(a_cross_b)]

        return tuple(int(math.ceil(2*cutoff/x)) for x in widths)

    def multiply(self, tuple):
        self._cell = np.multiply(self._cell.T, tuple).T
        self.__mkparam()
        self.__mklammps()
        self._inverse = np.linalg.inv(self._cell.T)

    @property
    def minimum_width(self):
        """The shortest perpendicular distance within the cell."""
        a_cross_b = cross(self.cell[0], self.cell[1])
        b_cross_c = cross(self.cell[1], self.cell[2])
        c_cross_a = cross(self.cell[2], self.cell[0])

        volume = dot(self.cell[0], b_cross_c)

        return volume / min(np.linalg.norm(b_cross_c), np.linalg.norm(c_cross_a), np.linalg.norm(a_cross_b))

    @property
    def inverse(self):
        """Inverted cell matrix for converting to fractional coordinates."""
        try:
            if self._inverse is None:
                self._inverse = np.linalg.inv(self.cell.T)
        except AttributeError:
            self._inverse = np.linalg.inv(self.cell.T)
        return self._inverse

    @property
    def crystal_system(self):
        """Return the IUCr designation for the crystal system."""
        #FIXME(tdaff): must be aligned with x to work
        if self.alpha == self.beta == self.gamma == 90:
            if self.a == self.b == self.c:
                return 'cubic'
            elif self.a == self.b or self.a == self.c or self.b == self.c:
                return 'tetragonal'
            else:
                return 'orthorhombic'
        elif self.alpha == self.beta == 90:
            if self.a == self.b and self.gamma == 120:
                return 'hexagonal'
            else:
                return 'monoclinic'
        elif self.alpha == self.gamma == 90:
            if self.a == self.c and self.beta == 120:
                return 'hexagonal'
            else:
                return 'monoclinic'
        elif self.beta == self.gamma == 90:
            if self.b == self.c and self.alpha == 120:
                return 'hexagonal'
            else:
                return 'monoclinic'
        elif self.a == self.b == self.c and self.alpha == self.beta == self.gamma:
            return 'trigonal'
        else:
            return 'triclinic'

    def __mkcell(self):
        """Update the cell representation to match the parameters."""
        a_mag, b_mag, c_mag = self.params[:3]
        alpha, beta, gamma = [x * DEG2RAD for x in self.params[3:]]
        a_vec = np.array([a_mag, 0.0, 0.0])
        b_vec = np.array([b_mag * np.cos(gamma), b_mag * np.sin(gamma), 0.0])
        c_x = c_mag * np.cos(beta)
        c_y = c_mag * (np.cos(alpha) - np.cos(gamma) * np.cos(beta)) / np.sin(gamma)
        c_vec = np.array([c_x, c_y, (c_mag**2 - c_x**2 - c_y**2)**0.5])
        self._cell = np.array([a_vec, b_vec, c_vec])

    def __mkparam(self):
        """Update the parameters to match the cell."""
        cell_a = np.sqrt(sum(x**2 for x in self.cell[0]))
        cell_b = np.sqrt(sum(x**2 for x in self.cell[1]))
        cell_c = np.sqrt(sum(x**2 for x in self.cell[2]))
        alpha = np.arccos(sum(self.cell[1, :] * self.cell[2, :]) /
                       (cell_b * cell_c)) * 180 / np.pi
        beta = np.arccos(sum(self.cell[0, :] * self.cell[2, :]) /
                      (cell_a * cell_c)) * 180 / np.pi
        gamma = np.arccos(sum(self.cell[0, :] * self.cell[1, :]) /
                       (cell_a * cell_b)) * 180 / np.pi
        self._params = (cell_a, cell_b, cell_c, alpha, beta, gamma)

    def __mklammps(self):
        a, b, c, alpha, beta, gamma = self._params
        lx = a
        xy = b*math.cos(gamma*DEG2RAD)
        xz = c*math.cos(beta*DEG2RAD)
        ly = math.sqrt(b**2 - xy**2)
        yz = (b*c*math.cos(alpha*DEG2RAD) - xy*xz)/ly
        lz = math.sqrt(c**2 - xz**2 - yz**2)
        self._lammps = (lx, ly, lz, xy, xz, yz)

    @property
    def lx(self):
        return self._lammps[0]
    @property
    def ly(self):
        return self._lammps[1]
    @property
    def lz(self):
        return self._lammps[2]
    @property
    def xy(self):
        return self._lammps[3]
    @property
    def xz(self):
        return self._lammps[4]
    @property
    def yz(self):
        return self._lammps[5]

    @property
    def a(self):
        """Magnitude of cell a vector."""
        return self.params[0]

    @property
    def b(self):
        """Magnitude of cell b vector."""
        return self.params[1]

    @property
    def c(self):
        """Magnitude of cell c vector."""
        return self.params[2]

    @property
    def alpha(self):
        """Cell angle alpha."""
        return self.params[3]

    @property
    def beta(self):
        """Cell angle beta."""
        return self.params[4]

    @property
    def gamma(self):
        """Cell angle gamma."""
        return self.params[5]

