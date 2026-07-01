"""Flash quickstart hello-world — sanity check the SDK/account works at all,
independent of vLLM/custom-image complexity. From docs.runpod.io/flash/quickstart.
"""
import asyncio
import forge
from runpod_flash import Endpoint, GpuGroup


@Endpoint(
    name="flash-quickstart",
    gpu=GpuGroup.ANY,
    workers=3,
    idle_timeout=300,
    dependencies=["numpy", "torch"],
)
def gpu_matrix_multiply(size):
    import numpy as np
    import torch

    device_name = torch.cuda.get_device_name(0)
    A = np.random.rand(size, size)
    B = np.random.rand(size, size)
    C = np.dot(A, B)

    return {
        "matrix_size": size,
        "result_mean": float(np.mean(C)),
        "gpu": device_name,
    }


async def main():
    forge.load_env("prod")
    print("Running matrix multiplication on Runpod GPU...")
    result = await gpu_matrix_multiply(1000)

    print(f"\n✓ Matrix size: {result['matrix_size']}x{result['matrix_size']}")
    print(f"✓ Result mean: {result['result_mean']:.4f}")
    print(f"✓ GPU used: {result['gpu']}")


if __name__ == "__main__":
    asyncio.run(main())
