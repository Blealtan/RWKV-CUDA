import torch
from torch.utils.cpp_extension import load
from torch.nn import functional as F
import numpy as np
from math import exp
np.set_printoptions(precision=4, suppress=True, linewidth=200)
# turn off TF32 for higher accuracy
torch.backends.cudnn.benchmark = True
torch.backends.cudnn.allow_tf32 = False
torch.backends.cuda.matmul.allow_tf32 = False

# os.environ["CUDA_VISIBLE_DEVICES"] = "1"
DEVICE = 'cuda'
CUDA_KERNEL_VERSION = 'v1'
DTYPE = torch.bfloat16

B = 8
T = 4096
C = 4096
HEAD_SIZE = 64
H = C // HEAD_SIZE

def set_seed(seed):
    np.random.seed(seed)
    torch.manual_seed(seed)
    torch.cuda.manual_seed_all(seed)

def get_err_ratio(x, y):
    err = (x-y).flatten().square().mean().sqrt().item()
    base = (x).flatten().square().mean().sqrt().item()
    return err / base

def val(x):
    return x.detach().cpu().numpy()

########################################################################################################
# CUDA Kernel
########################################################################################################

wkv_cuda = load(name="wkv5", sources=["cuda/wkv5_op.cpp", f"cuda/wkv5_cuda_{CUDA_KERNEL_VERSION}.cu"],
                verbose=True, extra_cuda_cflags=["-std=c++17", "-res-usage", "--use_fast_math", "-O3", "-Xptxas -O3", "--extra-device-vectorization", f"-DN={HEAD_SIZE}"])
    
class WKV_5(torch.autograd.Function):
    @staticmethod
    def forward(ctx, B, T, C, H, r, k, v, w, u):
        assert HEAD_SIZE == C // H
        assert r.dtype == torch.bfloat16
        assert k.dtype == torch.bfloat16
        assert v.dtype == torch.bfloat16
        assert w.dtype == torch.bfloat16
        assert u.dtype == torch.bfloat16
        ctx.B = B
        ctx.T = T
        ctx.C = C
        ctx.H = H
        r = r.contiguous()
        k = k.contiguous()
        v = v.contiguous()
        w = w.float().contiguous()
        u = u.contiguous()
        ew = -torch.exp(w)
        eew = torch.exp(ew)
        ctx.save_for_backward(r, k, v, eew, ew, u)
        y = torch.zeros((B, T, C), device='cuda', dtype=torch.bfloat16).contiguous()
        wkv_cuda.forward(B, T, C, H, r, k, v, eew, u, y)
        return y

    @staticmethod
    def backward(ctx, gy):
        B = ctx.B
        T = ctx.T
        C = ctx.C
        H = ctx.H
        gy = gy.contiguous()
        assert gy.dtype == torch.bfloat16
        r, k, v, eew, ew, u = ctx.saved_tensors
        gr = torch.zeros((B, T, C), device='cuda', requires_grad=False, dtype=torch.bfloat16)
        gk = torch.zeros((B, T, C), device='cuda', requires_grad=False, dtype=torch.bfloat16)
        gv = torch.zeros((B, T, C), device='cuda', requires_grad=False, dtype=torch.bfloat16)
        gw = torch.zeros((H, C//H), device='cuda', requires_grad=False, dtype=torch.bfloat16)
        gu = torch.zeros((H, C//H), device='cuda', requires_grad=False, dtype=torch.bfloat16)
        wkv_cuda.backward(B, T, C, H, r, k, v, eew, ew, u, gy, gr, gk, gv, gw, gu)
        gw = torch.sum(gw, dim=0).flatten()
        gu = torch.sum(gu, dim=0).flatten()
        return (None, None, None, None, gr, gk, gv, gw, gu)

def RUN_CUDA(B, T, C, H, r, k, v, w, u):
    return WKV_5.apply(B, T, C, H, r.cuda(), k.cuda(), v.cuda(), w.cuda(), u.cuda())

######################################################################################################
# Original pytorch version (requires w & u to be constant within each head)
######################################################################################################

class RUN_TORCH(torch.jit.ScriptModule):
    def __init__(self, chunk_len):
        super().__init__()
        self.chunk_len = chunk_len

    @torch.jit.script_method
    def jit_func(self, r, k, v, w, wk, wb, ws):
        B, T, C = r.size()
        H = w.size()[1]
        Z = self.chunk_len
        N = C // H
        r = r.view(B, T, H, N).transpose(1, 2) # BTC -> BHTN
        k = k.view(B, T, H, N).transpose(1, 2).transpose(-2, -1) # BTC -> BHTN -> BHNT
        v = v.view(B, T, H, N).transpose(1, 2) # BTC -> BHTN

        s = torch.zeros(B, H, N, N, device=r.device, dtype=r.dtype) # state
        x = torch.zeros(B, H, T, N, device=r.device, dtype=r.dtype) # output

        for i in range(T // Z):
            rr = r[:, :, i*Z:i*Z+Z, :]
            kk = k[:, :, :, i*Z:i*Z+Z]
            vv = v[:, :, i*Z:i*Z+Z, :]
            x[:, :, i*Z:i*Z+Z, :] = ((rr @ kk) * w) @ vv  +  (rr @ s) * wb
            s = ws * s + (kk * wk) @ vv

        return x.transpose(1, 2).contiguous().view(B, T, C) # BHTN -> BTHN -> BTC

    def forward(self, B, T, C, H, r, k, v, w, u):
        w = w.view(H, 1)
        u = u.view(H, 1)
        Z = self.chunk_len

        ws = w.pow(Z).reshape(1, H, 1, 1)

        ind = torch.arange(Z-1, -1, -1, device=r.device).unsqueeze(0).repeat(H, 1)
        w = w.repeat(1, Z).pow(ind)

        wk = w.reshape(1, H, 1, Z)
        wb = wk.transpose(-2, -1).flip(2)

        w = torch.cat([w[:, 1:], u], dim=1)
        w = F.pad(w, (0, Z))
        w = torch.tile(w, [Z])
        w = w[:, :-Z].reshape(-1, Z, 2 * Z - 1)
        w = w[:, :, Z-1:].reshape(1, H, Z, Z)

        w = w.to(dtype=r.dtype)
        wk = wk.to(dtype=r.dtype)
        wb = wb.to(dtype=r.dtype)
        ws = ws.to(dtype=r.dtype)

        return self.jit_func(r, k, v, w, wk, wb, ws)

######################################################################################################
# Check correctness
######################################################################################################

def CHECK_BACKWARD():
    def LOSS(y): # a strange loss for better verification
        return ((y * y) - torch.tanh(y)).sum()

    # firstly check vs original cuda
    set_seed(42)
    with torch.no_grad():
        r = torch.zeros(B, T, C, requires_grad=True, device=DEVICE, dtype=DTYPE).uniform_(-1, 1)
        k = torch.zeros(B, T, C, requires_grad=True, device=DEVICE, dtype=DTYPE).uniform_(-1, 1)
        v = torch.zeros(B, T, C, requires_grad=True, device=DEVICE, dtype=DTYPE).uniform_(-1, 1)
        w = torch.zeros(H, requires_grad=True, device=DEVICE, dtype=DTYPE).uniform_(-1, 1)
        u = torch.zeros(H, requires_grad=True, device=DEVICE, dtype=DTYPE).uniform_(-1, 1)    
    
    print(f'B={B} T={T} C={C} HEAD_SIZE={HEAD_SIZE}')
    assert T % 512 == 0
    print('[original torch (const w & u within a head)] vs [current cuda]')
    rwkv5_torch = RUN_TORCH(chunk_len = 512)

    y0 = rwkv5_torch.forward(B, T, C, H, r, k, v, torch.exp(-torch.exp(w.float())), u)
    y0 = rwkv5_torch.forward(B, T, C, H, r, k, v, torch.exp(-torch.exp(w.float())), u)
    with torch.autograd.profiler.profile(use_cuda=True) as prof:
        y0 = rwkv5_torch.forward(B, T, C, H, r, k, v, torch.exp(-torch.exp(w.float())), u)
    print('Torch forward\n', prof.key_averages(group_by_stack_n=5).table(
        sort_by='self_cuda_time_total', row_limit=5))
        
    ww = w.repeat_interleave(HEAD_SIZE)
    uu = u.repeat_interleave(HEAD_SIZE)

    y1 = RUN_CUDA(B, T, C, H, r, k, v, ww, uu)
    y1 = RUN_CUDA(B, T, C, H, r, k, v, ww, uu)
    with torch.autograd.profiler.profile(use_cuda=True) as prof:
        y1 = RUN_CUDA(B, T, C, H, r, k, v, ww, uu)
    print('CUDA forward\n', prof.key_averages(group_by_stack_n=5).table(
        sort_by='self_cuda_time_total', row_limit=5))
    
    print('--> correct =', torch.allclose(y0, y1),
        ', err ratio =', get_err_ratio(y0, y1))

    with torch.autograd.profiler.profile(use_cuda=True) as prof:
        LOSS(y0).backward()
    print('Torch backward\n', prof.key_averages(group_by_stack_n=5).table(
        sort_by='self_cuda_time_total', row_limit=5))
            
    gr0 = r.grad.data.clone()
    gk0 = k.grad.data.clone()
    gv0 = v.grad.data.clone()
    gw0 = w.grad.data.clone()
    gu0 = u.grad.data.clone()
    r.grad.data.zero_()
    k.grad.data.zero_()
    v.grad.data.zero_()
    w.grad.data.zero_()
    u.grad.data.zero_()
    with torch.autograd.profiler.profile(use_cuda=True) as prof:
        LOSS(y1).backward()
    print('CUDA backward\n', prof.key_averages(group_by_stack_n=5).table(
        sort_by='self_cuda_time_total', row_limit=5))
        
    gr1 = r.grad.data.clone()
    gk1 = k.grad.data.clone()
    gv1 = v.grad.data.clone()
    gw1 = w.grad.data.clone()
    gu1 = u.grad.data.clone()            
    print('--> g_r correct =', torch.allclose(gr0, gr1), ', err ratio =', get_err_ratio(gr0, gr1))
    print('--> g_k correct =', torch.allclose(gk0, gk1), ', err ratio =', get_err_ratio(gk0, gk1))
    print('--> g_v correct =', torch.allclose(gv0, gv1), ', err ratio =', get_err_ratio(gv0, gv1))
    print('--> g_w correct =', torch.allclose(gw0, gw1), ', err ratio =', get_err_ratio(gw0, gw1))
    print('--> g_u correct =', torch.allclose(gu0, gu1), ', err ratio =', get_err_ratio(gu0, gu1))

CHECK_BACKWARD()