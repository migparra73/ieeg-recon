from email import header
from nipype.interfaces import (
    utility as niu,
    freesurfer as fs,
    fsl,
    image,
)

import pathlib

from nipype import Node, Workflow, SelectFiles, MapNode
from nipype.interfaces.utility import Function

import nipype.interfaces.io as nio
import os
from nipype.interfaces.ants.base import Info as ANTsInfo
from nipype.interfaces.ants import N4BiasFieldCorrection
from nipype.interfaces.image import Reorient

import numpy as np
import nibabel as nib
import ants
import antspynet
from mayavi import mlab

## Add the Command Line Interface
from nipype.interfaces.base import CommandLineInputSpec, File, TraitedSpec,  CommandLine
from nipype.interfaces.c3 import C3dAffineTool

## Argument Parser
import argparse
parser = argparse.ArgumentParser()

# import module 3 atlas finder

#-db DATABSE -u USERNAME -p PASSWORD -size 20
parser.add_argument("-s", "--subject", help="Subject ID")
parser.add_argument("-d", "--source_directory", help="Source Directory")
parser.add_argument("-rs","--reference_session")
parser.add_argument("-fs","--freesurfer_dir")
args = parser.parse_args()


print(args.subject)


#subjects = ['sub-RID0031','sub-RID0051','sub-RID0089','sub-RID0102','sub-RID0117','sub-RID0139','sub-RID0143','sub-RID0194','sub-RID0278','sub-RID0309','sub-RID0320','sub-RID0365','sub-RID0420','sub-RID0440','sub-RID0454','sub-RID0476','sub-RID0490','sub-RID0508','sub-RID0520','sub-RID0522','sub-RID0536','sub-RID0566','sub-RID0572','sub-RID0595','sub-RID0646','sub-RID0648','sub-RID0679']

subject = args.subject


#subjects = ['sub-RID0139']

source_dir = args.source_directory
reference_session = args.reference_session
mod2_folder = os.path.join(source_dir,subject,'derivatives','ieeg_recon', 'module2')
brainshift_folder = os.path.join(mod2_folder,'brainshift')
os.makedirs(brainshift_folder)

clinical_module_dir = mod2_folder
mod3_folder = os.path.join(source_dir,subject,'derivatives','ieeg_recon', 'module3','brain_shift')
freesurfer_dir = args.freesurfer_dir

# Load the MRI
if os.path.exists(os.path.join(clinical_module_dir,'MRI_RAS', subject+'_'+reference_session+'_acq-3D_space-T00mri_T1w.nii.gz')):
    img_path = os.path.join(clinical_module_dir,'MRI_RAS', subject+'_'+reference_session+'_acq-3D_space-T00mri_T1w.nii.gz')
else:
    img_path = os.path.join(clinical_module_dir, subject+'_'+reference_session+'_acq-3D_space-T00mri_T1w_ras.nii.gz')

# Load the freesurfer data
lh_pial = nib.freesurfer.read_geometry(os.path.join(freesurfer_dir,'surf/lh.pial'))
rh_pial = nib.freesurfer.read_geometry(os.path.join(freesurfer_dir,'surf/rh.pial'))

# Load the freesurfer mesh
vertices_lh, triangles_lh = lh_pial
vertices_rh, triangles_rh = rh_pial

vertices = np.vstack([vertices_lh, vertices_rh])
triangles = np.vstack([triangles_lh, triangles_rh+len(vertices_lh)])

# Load the freesurfer and iEEG-recon MRIs, we need them for their affines
volume_fs = nib.load(os.path.join(freesurfer_dir,'mri/T1.mgz'))
volume_recon = nib.load(img_path)

# get transform from FreeSurfer MRI Surface RAS to iEEG-recon MRI Voxel Space
Torig = volume_fs.header.get_vox2ras_tkr()
affine_fs = volume_fs.affine
affine_target = volume_recon.affine
T = np.dot(np.linalg.inv(affine_target),np.dot(affine_fs,np.linalg.inv(Torig)))

# transform the vertices to the iEEG-recon MRI voxel space
def apply_affine(affine, coords):
    """Apply an affine transformation to 3D coordinates."""
    homogeneous_coords = np.hstack((coords, np.ones((coords.shape[0], 1))))
    transformed_coords = np.dot(homogeneous_coords, affine.T)
    return transformed_coords[:, :3]

transformed_vertices = apply_affine(T, vertices)

# load the electrode coordinates
electrode_coordinates = np.loadtxt(os.path.join(mod2_folder, subject+'_'+reference_session+'_space-T00mri_desc-vox_electrodes.txt'))


##### Apply electrode snapping to the pial surface using constraints and optimization ######

from scipy.optimize import minimize

def compute_alpha(e0):
    N = len(e0)
    alpha = np.zeros((N, N))
    distances = np.array([[np.linalg.norm(e0[i] - e0[j]) for j in range(N)] for i in range(N)])
    
    # Find the 5 nearest neighbors for each electrode
    nearest_neighbors = np.argsort(distances, axis=1)[:, 1:6]  # Exclude the diagonal (distance to self)
    
    # Quantize the distances and determine the bin with the largest count
    quantized_bins = (distances // 0.2).astype(int)
    bins_counts = np.bincount(quantized_bins.flatten())
    fundamental_distance_bin = np.argmax(bins_counts)
    fundamental_distance = fundamental_distance_bin * 0.2
    
    threshold = 1.25 * fundamental_distance
    
    for i in range(N):
        for j in nearest_neighbors[i]:
            if distances[i][j] < threshold:
                alpha[i][j] = 1
    
    # Ensure each electrode has at least one connection
    for i in range(N):
        if np.sum(alpha[i]) == 0:
            nearest_electrode = np.argmin(distances[i])
            alpha[i][nearest_electrode] = 1
            
            for j in range(N):
                if distances[i][j] < 1.25 * distances[i][nearest_electrode]:
                    alpha[i][j] = 1
    
    return alpha

e0 = electrode_coordinates
N = len(e0)
Nv = len(transformed_vertices)
e_s_distance_array = np.array([[np.linalg.norm(e0[i] - transformed_vertices[j]) for j in range(Nv)] for i in range(N)])

# Original distances between electrodes in a symmetric matrix form
d0 = np.array([[np.linalg.norm(e0[i] - e0[j]) for j in range(N)] for i in range(N)])

alpha = compute_alpha(e0)  # Random 0s and 1s for the alpha matrix

s = transformed_vertices[np.argmin(e_s_distance_array,axis=1)]

# define the objective function and run optimization

def objective(e_flat):
    e = e_flat.reshape(N, 3)
    
    # Computing the distances between the current electrode positions
    d = np.array([[np.linalg.norm(e[i] - e[j]) for j in range(N)] for i in range(N)])
    
    term1 = np.sum(np.square(e - e0))
    term2 = np.sum(alpha * ((d - d0)**2))
    
    return term1 + term2

def constraint(e_flat):
    e = e_flat.reshape(N, 3)
    return np.sum(np.square(e - s))

# Constraints in the form required by `minimize`
cons = {'type':'eq', 'fun': constraint}

# Initial guesses for e values
x0 = e0.flatten()

# Solve the optimization problem
result = minimize(objective, x0, constraints=cons)

optimized_e = result.x.reshape(N, 3)

#### Create a figure of before and after ####

# Before
mlab.triangular_mesh(transformed_vertices[:, 0], transformed_vertices[:, 1], transformed_vertices[:, 2], triangles, color=(1,0.85,0.85))
#mlab.triangular_mesh(vertices[:, 0], vertices[:, 1], vertices[:, 2], triangles)

# Assuming electrode_coordinates is already defined
for coord in electrode_coordinates:
    mlab.points3d(coord[0], coord[1], coord[2], scale_factor=4, color=(1, 0, 0)) # Scale factor is double the radius


# Define a function to adjust the camera view and save it
def save_view(azimuth, elevation, filename):
    mlab.view(azimuth=azimuth, elevation=elevation)
    mlab.savefig(filename)

# Save left, right, top, and bottom views
save_view(0, 0, os.path.join(brainshift_folder,'top_view_before.png'))
save_view(0, 180, os.path.join(brainshift_folder,'bottom_view_before.png'))
save_view(0, 90, os.path.join(brainshift_folder,'right_view_before.png'))
save_view(0, -90, os.path.join(brainshift_folder,'left_view_before.png'))


# After
mlab.triangular_mesh(transformed_vertices[:, 0], transformed_vertices[:, 1], transformed_vertices[:, 2], triangles, color=(1,0.85,0.85))
#mlab.triangular_mesh(vertices[:, 0], vertices[:, 1], vertices[:, 2], triangles)

# Assuming electrode_coordinates is already defined
for coord in optimized_e:
    mlab.points3d(coord[0], coord[1], coord[2], scale_factor=4, color=(1, 0, 0)) # Scale factor is double the radius


# Define a function to adjust the camera view and save it
def save_view(azimuth, elevation, filename):
    mlab.view(azimuth=azimuth, elevation=elevation)
    mlab.savefig(filename)

# Save left, right, top, and bottom views
save_view(0, 0, os.path.join(brainshift_folder,'top_view_after.png'))
save_view(0, 180, os.path.join(brainshift_folder,'bottom_view_after.png'))
save_view(0, 90, os.path.join(brainshift_folder,'right_view_after.png'))
save_view(0, -90, os.path.join(brainshift_folder,'left_view_after.png'))

#### Rename the module 2 outputs to before brainshift

os.rename(os.path.join(mod2_folder, subject+'_'+reference_session+'_space-T00mri_desc-vox_electrodes.txt'),
          os.path.join(mod2_folder, subject+'_'+reference_session+'_space-T00mri_desc-vox_electrodes_before_brainshift.txt'))

os.rename(os.path.join(mod2_folder, subject+'_'+reference_session+'_acq-3D_space-T00mri_T1w_electrode_spheres.nii.gz'),
          os.path.join(mod2_folder, subject+'_'+reference_session+'_acq-3D_space-T00mri_T1w_electrode_spheres_before_brainshift.nii.gz'))

#### Save the new module 2 outputs after brainshift

# save the new voxel coordinates
np.savetxt(os.path.join(mod2_folder, subject+'_'+reference_session+'_space-T00mri_desc-vox_electrodes.txt'), optimized_e)

# save the new electrode spheres
v = volume_recon.get_fdata()
new_spheres = np.zeros(v.shape, dtype=np.float64)

def generate_sphere(A, x0,y0,z0, radius, value):
    ''' 
        A: array where the sphere will be drawn
        radius : radius of circle inside A which will be filled with ones.
        x0,y0,z0: coordinates for the center of the sphere within A
        value: value to fill the sphere with
    '''

    ''' AA : copy of A (you don't want the original copy of A to be overwritten.) '''
    AA = A



    for x in range(x0-radius, x0+radius+1):
        for y in range(y0-radius, y0+radius+1):
            for z in range(z0-radius, z0+radius+1):
                ''' deb: measures how far a coordinate in A is far from the center. 
                        deb>=0: inside the sphere.
                        deb<0: outside the sphere.'''   
                deb = radius - ((x0-x)**2 + (y0-y)**2 + (z0-z)**2)**0.5 
                if (deb)>=0: AA[x,y,z] = value
    return AA

for coord in optimized_e:
    new_spheres = generate_sphere(new_spheres, int(coord[0]), int(coord[1]), int(coord[2]), 2, 1)

nib.save(nib.Nifti1Image(new_spheres, volume_recon.affine),os.path.join(mod2_folder, subject+'_'+reference_session+'_acq-3D_space-T00mri_T1w_electrode_spheres.nii.gz'))