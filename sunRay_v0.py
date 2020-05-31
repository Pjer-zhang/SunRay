import numpy as np
#import matplotlib.pyplot as plt
from sunRay import plasmaFreq as pfreq
from sunRay import densityModel as dm
from sunRay import scattering as scat 
from sunRay import showPlot as SP
from sunRay.parameters import dev_u # use GPU if available
import torch
import time
from tqdm import tqdm # for processing bar

torch.set_num_threads(40)
torch.set_default_tensor_type(torch.DoubleTensor)

# initialize
steps_N  = -1;        # number of the step # set as -1 to autoset
collect_N = 300;      # number of recorded step
t_param = 20.0;       # parameter of t step length
# larger t_parm corresponding to smaller dt

photon_N = 100000      # number of photon
start_r = 1.75;       # in solar radii
start_theta = 1/180.0*np.pi;    # in rad
start_phi  = 0/180.0*np.pi;     # in rad

R_S = 6.96e10         # the radius of the sun 
c   = 2.998e10        # speed of light
c_r = c/R_S           # [t]

f_ratio  = 1.1        # f/f_pe
ne_r = dm.parkerfit   # use leblanc for this calculation 
epsilon = 0.5         # fluctuation scale
anis = 0.3            # the anisotropic parameter
asym = 1.0            # asymetric scale

Te = 86.0             # eV temperature in eV

# debug parameters

Scat_include = True   # whether to consider the  

Show_param = True      # Display the parameters
Show_result_k = False  # Show simulation result k
Show_result_r = True   # Show simulation result r
verb_out = False       # print message

sphere_gen = False
  

# put variable in device
start_r = torch.tensor([start_r])  
PI = torch.acos(torch.Tensor([-1])).to(dev_u) # pi
nu_e0 = 2.91e-6*ne_r(start_r)*20./Te**1.5
nu_e = nu_e0


# frequency of the wave
freq0 = f_ratio * pfreq.omega_pe_r(ne_r,start_r.to(dev_u))/(2*PI)
print('----------------------------------')
print('Frequency : '+str(freq0.cpu().data.numpy()/1e6)[1:7]+'MHz')
print('Compute with : '+str(dev_u))
print('----------------------------------')

#freq0 = torch.Tensor([freq0]).to(dev_u)

# position of the photons
rxx = start_r * torch.Tensor(np.sin(start_theta) * np.cos(start_phi) * np.ones(photon_N))
ryy = start_r * torch.Tensor(np.sin(start_theta) * np.sin(start_phi) * np.ones(photon_N))
rzz = start_r * torch.Tensor(np.cos(start_theta) * np.ones(photon_N))
rr = start_r.to(dev_u) * torch.ones(photon_N).to(dev_u)
rr_cur = rr # rr_cur [current rr for for loop]
r_vec = torch.stack((rxx,ryy,rzz),0).to(dev_u)

omega0 = freq0*(2*PI)
nu_s0 = scat.nuScattering(rr,omega0,epsilon,ne_r)

if Show_param:
    SP.showParameters(ne_r,omega0,epsilon)  

# wave-vector of the photons
kc0 = torch.sqrt(omega0**2. - pfreq.omega_pe_r(ne_r,rr)**2.)
if sphere_gen:
    k_theta = torch.Tensor(np.random.uniform(low=-np.pi/2 + 1e-4 ,
                            high= np.pi/2 ,size=photon_N)).to(dev_u) # k_z > 0 
    k_mu0   = torch.cos(k_theta)
    k_phi0  = torch.Tensor(np.random.uniform(low=0 ,
                            high= 2*np.pi, size=photon_N)).to(dev_u) # phi in all dir

    kxx_k = kc0 * torch.sqrt(1-k_mu0**2.) * torch.cos(k_phi0)
    kyy_k = kc0 * torch.sqrt(1-k_mu0**2.) * torch.sin(k_phi0)
    kzz_k = kc0 * k_mu0
    k_vec = torch.stack((kxx_k,kyy_k,kzz_k),0).to(dev_u)
else:
    # generate in xyz
    k_vec_tmp = torch.randn(3,photon_N).to(dev_u)
    k_vec = kc0 * k_vec_tmp/torch.sqrt(torch.sum(k_vec_tmp.pow(2),axis=0))
    # ignore downward (r k not same direction)
    idx_select = torch.nonzero(torch.sum(r_vec*k_vec,axis=0)<0,as_tuple=False)
    k_vec[:,idx_select] = -k_vec[:,idx_select] 

r_vec_start = r_vec
k_vec_start = k_vec


kc = torch.sqrt(torch.sum(k_vec.pow(2),axis=0))
kc_cur = kc

# Detach from the previous compute graph
# before record steps for diff
domega_pe_dxyz = pfreq.domega_dxyz(ne_r,r_vec.detach())

Exp_size = 1.25*30./(freq0/1e6)
dt0 = 0.01*Exp_size/c_r
tau = torch.zeros(rr_cur.shape).to(dev_u)

# collect the variables of the simulation
collectPoints = np.round(np.linspace(0,steps_N-1,collect_N))
r_vec_collect = torch.zeros(collect_N,3,photon_N).to(dev_u)
k_vec_collect = torch.zeros(collect_N,3,photon_N).to(dev_u)
t_collect = torch.zeros(collect_N).to(dev_u)
idx_collect  =  0
t_current = 0

if steps_N == -1:
    dt_dr0  = find_small_1e4(rr_cur/omega0*kc_cur)/t_param
    dt_nu0  = find_small_1e4(1.0/(nu_s0)) 
    steps_N = (3*4.605/nu_e0 + 30*c_r)*(1/dt_dr0+1/dt_nu0)

# a function to find the 1/1e4 small element in the array
find_small_1e4 = lambda arr:  torch.sort(arr)[0][int(photon_N*1e-4)]

# the big loop
for idx_step in tqdm(np.arange(steps_N)):
    
    # dispersion relation reform
    omega = torch.sqrt(pfreq.omega_pe_r(ne_r,rr_cur)**2 + kc_cur**2)
    freq_pe = omega/(2*PI)

    nu_s = scat.nuScattering(rr_cur,omega,epsilon,ne_r)
    nu_s = nu_s*(nu_s<nu_s0)+nu_s0*(~(nu_s<nu_s0)) # use the smaller nu_s

    # compare the diff of the CPU and GPU

    domega_pe_dxyz = pfreq.domega_dxyz(ne_r,r_vec.detach())
    domega_pe_dr = torch.sqrt(torch.sum(domega_pe_dxyz.pow(2),axis=0))

    with torch.no_grad(): # no autograd in following calc
        dr_vec = c_r/omega.repeat(3,1) * k_vec    

        # component of r and k vector at current step
        rr_cur = torch.sqrt(torch.sum(r_vec.pow(2),axis=0))
        kc_cur = torch.sqrt(torch.sum(k_vec.pow(2),axis=0))
        rx_cur,ry_cur,rz_cur = r_vec[0,:],r_vec[1,:],r_vec[2,:]
        kx_cur,ky_cur,kz_cur = k_vec[0,:],k_vec[1,:],k_vec[2,:]


        # dynamic time step
        dt_ref = find_small_1e4(torch.abs(kc_cur/ (domega_pe_dr*c_r)/t_param)) # t step
        dt_dr  = find_small_1e4(rr_cur/omega0*kc_cur)/t_param
        dt_nu  = find_small_1e4(1.0/(nu_s)) 
        # make sure most of the photons have proper dt 
        dt = torch.Tensor([np.nanmin([dt_nu,dt_ref,dt_dr,dt0])]).to(dev_u)
        
        g0 = torch.sqrt(nu_s*kc_cur**2)

        # random vec for wave scattering  # [3*N] normal distribution
        W_vec = torch.randn(r_vec.shape).to(dev_u) * torch.sqrt(dt) 
        Wx,Wy,Wz = W_vec[0,:],W_vec[1,:],W_vec[2,:]

        # photon position in spherical coordinates
        # (rx,ry,rz) is the direction of anisotropic tubulence
        fi = torch.atan(ry_cur/rx_cur)
        costheta = rz_cur/rr_cur
        sintheta = torch.sqrt(1-costheta**2)
        if Scat_include:

            # rotate the k vec into the r-z coordinate
            kcx = - kx_cur*torch.sin(fi) + ky_cur*torch.cos(fi) 
            kcy = (- kx_cur*costheta*torch.cos(fi) 
                - ky_cur*costheta*torch.sin(fi) + kz_cur*sintheta) 
            kcz = (  kx_cur*sintheta*torch.cos(fi) 
                + ky_cur*sintheta*torch.sin(fi) + kz_cur*costheta)

            kw     =  Wx*kcx+Wy*kcy+Wz*kcz*anis
            Akc    = torch.sqrt(kcx*kcx+kcy*kcy+kcz*kcz*(anis**2))
            z_asym = (asym*(kcz > 0.0) + (2.0-asym)*(~(kcz>0.))) * (kc_cur/Akc)**2

            A_perp = (nu_s*z_asym* kc_cur /(Akc**3) *
                (-(1+anis**2)*Akc**2+3*anis**2 *(anis**2-1)*kcz**2) *anis)
            A_par  = (nu_s*z_asym* kc_cur /(Akc**3) *
                ((-3*anis**4+anis**2)*(Akc**2)+3*anis**4 * (anis**2-1)*kcz**2)*anis)
            A_g0   = g0*torch.sqrt(z_asym*anis)

            kcx = kcx + A_perp*kcx*dt + A_g0*(Wx-kcx*kw/Akc**2)
            kcy = kcy + A_perp*kcy*dt + A_g0*(Wy-kcy*kw/Akc**2)
            kcz = kcz + A_par *kcz*dt + A_g0*(Wz-kcz*kw*anis/Akc**2)*anis

            # rotate back to normal coordinate
            kx_cur = (-kcx*torch.sin(fi) 
                -kcy*costheta*torch.cos(fi) +kcz*sintheta*torch.cos(fi) )
            ky_cur = ( kcx*torch.cos(fi) 
                -kcy*costheta*torch.sin(fi) +kcz*sintheta*torch.sin(fi) )
            kz_cur =  kcy*sintheta+kcz*costheta


        r_vec = torch.stack((rx_cur,ry_cur,rz_cur),0)
        k_vec = torch.stack((kx_cur,ky_cur,kz_cur),0)

        rr_cur = torch.sqrt(torch.sum(r_vec.pow(2),axis=0))
        kc_cur = torch.sqrt(torch.sum(k_vec.pow(2),axis=0))

        # re-normalize  # to keep |k_vec| stable
        kc_norm = torch.sqrt(kx_cur**2 + ky_cur**2 + kz_cur**2)
        k_vec = k_vec * kc_cur.repeat(3,1)/ kc_norm.repeat(3,1)

        # k step forward  # refraction
        dk_xyz_dt = ((pfreq.omega_pe_r(ne_r,rr_cur)/omega).repeat(3,1)   
                    * domega_pe_dxyz) * c_r
        k_vec = k_vec - dk_xyz_dt * dt

        # r step forward
        r_vec = r_vec + dr_vec * dt

        # update abs after vec change        
        rr_cur = torch.sqrt(torch.sum(r_vec.pow(2),axis=0))
        kc_cur = torch.sqrt(torch.sum(k_vec.pow(2),axis=0))

        # re-normalize  # to keep omega stable
        kc_refresh = (torch.sqrt(omega**2-pfreq.omega_pe_r(ne_r,rr_cur)**2)
            /torch.sqrt(torch.sum(k_vec.pow(2),axis=0)))
        k_vec = k_vec * kc_refresh.repeat(3,1)

        nu_e = (2.91e-6*ne_r(rr_cur)*20./Te**1.5
            *pfreq.omega_pe_r(ne_r, rr_cur)**2/omega**2)

        tau = tau + nu_e*dt

        rr_cur = torch.sqrt(torch.sum(r_vec.pow(2),axis=0))
        kc_cur = torch.sqrt(torch.sum(k_vec.pow(2),axis=0))

        # absorb the large tau photon (set as "not a number(nan)")
        if torch.argmax(tau)>4.605:
            idx_absorb = torch.nonzero(tau>4.605,as_tuple=False)
            r_vec[:,idx_absorb] = r_vec[:,idx_absorb]*torch.Tensor([np.nan]).to(dev_u) 
            k_vec[:,idx_absorb] = k_vec[:,idx_absorb]*torch.Tensor([np.nan]).to(dev_u) 
            rr_cur[idx_absorb] =  rr_cur[idx_absorb]*torch.Tensor([np.nan]).to(dev_u) 
            kc_cur[idx_absorb] =  kc_cur[idx_absorb]*torch.Tensor([np.nan]).to(dev_u)

        
    t_current = t_current + dt
    if idx_step in collectPoints:
        t_collect[idx_collect] = t_current
        r_vec_collect[idx_collect,:,:] = r_vec
        k_vec_collect[idx_collect,:,:] = k_vec
        idx_collect = idx_collect +1
        if verb_out: # print out the process
            print('F_pe:'+'{:.3f}'.format(np.mean(
                (pfreq.omega_pe_r(ne_r,torch.mean(rr_cur))/2/PI/1e6).cpu().data.numpy()))+
                ' |  R:'+'{:.3f}'.format(torch.mean(rr_cur).cpu().data.numpy())+
                ' |  Ne_r:'+'{:.3f}'.format(ne_r(torch.mean(rr_cur)).cpu().data.numpy())+
                ' |  nu_s: ' +  '{:.3f}'.format(torch.mean(0.1/nu_s).cpu().data.numpy())+
                ' |  F_ratio: ' +  '{:.3f}'.format(torch.mean(omega0/omega).cpu().data.numpy()))

t_collect_local = t_collect.cpu().data.numpy()
r_vec_collect_local  = r_vec_collect.cpu().data.numpy()
k_vec_collect_local  = k_vec_collect.cpu().data.numpy()

#plt.figure(1)
#plt.plot(t_collect_local, r_vec_collect_local[:,0,0])
#plt.figure(2)
#plt.plot( r_vec_collect_local[:,0,0], r_vec_collect_local[:,1,0])

print('Traced final t : '+str(t_collect_local[-1])+' s')

if Show_result_r:
    SP.showResultR(r_vec_collect_local)