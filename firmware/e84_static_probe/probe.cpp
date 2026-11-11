#include <stdint.h>
#include <string.h>

#include <rtthread.h>
#include <rthw.h>
#include "mtb_ml.h"
#include "tensorflow/lite/micro/micro_interpreter.h"
#include "tensorflow/lite/micro/micro_mutable_op_resolver.h"
#include "tensorflow/lite/schema/schema_generated.h"

#include "probe_assets.h"

extern "C" {
extern const uint8_t tcm_probe_model[TCM_MODEL_BYTES];
#if TCM_SAMPLE_COUNT == 1
extern const int8_t tcm_probe_input[TCM_SAMPLE_COUNT * TCM_INPUT_BYTES];
#else
struct rt_memheap* drv_hyperam_get_memheap(void);
#endif
}

namespace {
// Provisioning for the first boot, not a measurement of arena usage.
alignas(32) uint8_t arena[1792 * 1024]
    __attribute__((section(".cy_ml_arena_fast_data")));

bool in_region(const void* buffer, size_t bytes, uintptr_t base, size_t size)
{
    const uintptr_t address = reinterpret_cast<uintptr_t>(buffer);
    return address >= base && bytes <= size && address - base <= size - bytes;
}

bool matches(const TfLiteTensor* tensor, size_t bytes, const int* shape,
             int rank, float scale, int zero_point)
{
    if (!tensor || tensor->type != kTfLiteInt8 || !tensor->data.int8 ||
        !tensor->dims || tensor->dims->size != rank || tensor->bytes != bytes ||
        tensor->params.scale != scale || tensor->params.zero_point != zero_point) {
        return false;
    }
    for (int i = 0; i < rank; ++i) {
        if (tensor->dims->data[i] != shape[i]) return false;
    }
    return true;
}

int fail(const char* stage)
{
    rt_kprintf("TCM_PROBE_ERROR stage=%s\r\n", stage);
    return 1;
}

int run_model(const int8_t* samples)
{
    if (memcmp(tcm_probe_model + 4, "TFL3", 4) != 0) return fail("identifier");
    const tflite::Model* model = tflite::GetModel(tcm_probe_model);
    if (model->version() != TFLITE_SCHEMA_VERSION) return fail("schema");

    tflite::MicroMutableOpResolver<1> resolver;
    if (resolver.AddEthosU() != kTfLiteOk || !resolver.FindOp("ethos-u")) {
        return fail("ethos_u_registration");
    }
    tflite::MicroInterpreter interpreter(model, resolver, arena, sizeof(arena));
    if (interpreter.AllocateTensors() != kTfLiteOk) return fail("allocate");
    if (interpreter.inputs_size() != 1 || interpreter.outputs_size() != 1) {
        return fail("io_count");
    }
    auto* input = interpreter.input(0);
    auto* output = interpreter.output(0);
    const int input_shape[] = {1, 224, 224, 3};
    const int output_shape[] = {1, 20};
    if (!matches(input, TCM_INPUT_BYTES, input_shape, 4, TCM_INPUT_SCALE,
                 TCM_INPUT_ZERO_POINT) ||
        !matches(output, TCM_OUTPUT_BYTES, output_shape, 2, TCM_OUTPUT_SCALE,
                 TCM_OUTPUT_ZERO_POINT)) {
        return fail("io_contract");
    }

    constexpr char digits[] = "0123456789abcdef";
    for (unsigned sample = 0; sample < TCM_SAMPLE_COUNT; ++sample) {
        memcpy(input->data.int8, samples + sample * TCM_INPUT_BYTES, TCM_INPUT_BYTES);
        const rt_tick_t start = rt_tick_get();
        if (interpreter.Invoke() != kTfLiteOk) {
            rt_kprintf("TCM_PROBE_ERROR stage=invoke sample=%02u\r\n", sample);
            return 1;
        }
        const rt_tick_t ticks = rt_tick_get() - start;
        char hex[2 * TCM_OUTPUT_BYTES + 1];
        for (size_t i = 0; i < TCM_OUTPUT_BYTES; ++i) {
            const uint8_t q = static_cast<uint8_t>(output->data.int8[i]);
            hex[2 * i] = digits[q >> 4];
            hex[2 * i + 1] = digits[q & 15U];
        }
        hex[2 * TCM_OUTPUT_BYTES] = '\0';
        rt_kprintf("TCM_PROBE v=1 model=%s sample=%02u ticks=%u hz=%u "
                   "arena_used=%u arena_total=%u logits=%s\r\n",
                   TCM_MODEL_SHA256, sample, static_cast<unsigned>(ticks),
                   static_cast<unsigned>(RT_TICK_PER_SECOND),
                   static_cast<unsigned>(interpreter.arena_used_bytes()),
                   static_cast<unsigned>(sizeof(arena)), hex);
    }
    return 0;
}

#if TCM_SAMPLE_COUNT > 1
// The host writes the input bundle over SWD, then publishes this flag last.
constexpr uint32_t input_ready = 0x54434d32;
constexpr size_t line_size = 32;
constexpr size_t input_bytes = TCM_SAMPLE_COUNT * TCM_INPUT_BYTES;
constexpr uintptr_t hyperram_base = 0x64400000;
constexpr size_t hyperram_size = 0x800000;

uint32_t input_crc32(const uint8_t* data)
{
    uint32_t crc = ~0U;
    for (size_t i = 0; i < input_bytes; ++i) {
        crc ^= data[i];
        for (unsigned bit = 0; bit < 8; ++bit) {
            crc = (crc >> 1) ^ (0xedb88320U & (0U - (crc & 1U)));
        }
    }
    return ~crc;
}

int run_received_batch()
{
    auto* heap = drv_hyperam_get_memheap();
    if (!heap) return fail("input_heap");
    void* raw = rt_memheap_alloc(heap, input_bytes + 2 * line_size);
    if (!raw) return fail("input_allocation");
    auto* mailbox = reinterpret_cast<uint8_t*>((reinterpret_cast<uintptr_t>(raw) + line_size - 1) & ~(line_size - 1));
    uint8_t* payload = mailbox + line_size;
    int result = 1;
    if (!in_region(mailbox, input_bytes + line_size, hyperram_base, hyperram_size)) {
        result = fail("input_placement");
    } else {
        auto* flag = reinterpret_cast<volatile uint32_t*>(mailbox);
        *flag = 0;
        rt_hw_cpu_dcache_ops(RT_HW_CACHE_FLUSH, mailbox, line_size);
        rt_kprintf("TCM_PROBE_WAIT mailbox=0x%08x address=0x%08x bytes=%u inputs=%s\r\n",
                   static_cast<unsigned>(reinterpret_cast<uintptr_t>(mailbox)),
                   static_cast<unsigned>(reinterpret_cast<uintptr_t>(payload)),
                   static_cast<unsigned>(input_bytes), TCM_INPUTS_SHA256);
        bool ready = false;
        for (unsigned attempt = 0; attempt < 3000; ++attempt) {
            rt_hw_cpu_dcache_ops(RT_HW_CACHE_INVALIDATE, mailbox, line_size);
            if (*flag == input_ready) { ready = true; break; }
            rt_thread_mdelay(100);
        }
        if (!ready) {
            result = fail("input_timeout");
        } else {
            rt_hw_cpu_dcache_ops(RT_HW_CACHE_INVALIDATE, payload, input_bytes);
            if (input_crc32(payload) != TCM_INPUTS_CRC32) result = fail("input_crc32");
            else result = run_model(reinterpret_cast<const int8_t*>(payload));
        }
    }
    rt_memheap_free(raw);
    return result;
}
#endif
} // namespace

extern "C" int tcm_run_probe(void)
{
    rt_kprintf("TCM_PROBE_START samples=%u model=%s inputs=%s\r\n",
               TCM_SAMPLE_COUNT, TCM_MODEL_SHA256, TCM_INPUTS_SHA256);
    if (!in_region(tcm_probe_model, TCM_MODEL_BYTES, TCM_FLASH_BASE, TCM_FLASH_SIZE) ||
#if TCM_SAMPLE_COUNT == 1
        !in_region(tcm_probe_input, sizeof(tcm_probe_input), TCM_FLASH_BASE, TCM_FLASH_SIZE) ||
#endif
        !in_region(arena, sizeof(arena), TCM_SRAM_BASE, TCM_SRAM_SIZE)) {
        return fail("linker_placement");
    }
    if (mtb_ml_init(3U) != MTB_ML_RESULT_SUCCESS) return fail("npu_init");
#if TCM_SAMPLE_COUNT == 1
    const int result = run_model(tcm_probe_input);
#else
    const int result = run_received_batch();
#endif
    if (mtb_ml_deinit() != MTB_ML_RESULT_SUCCESS) return fail("npu_deinit");
    if (result == 0) {
        rt_kprintf("TCM_PROBE_DONE samples=%u inputs=%s\r\n", TCM_SAMPLE_COUNT, TCM_INPUTS_SHA256);
    }
    return result;
}
