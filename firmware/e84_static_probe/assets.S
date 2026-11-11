/* Assemble from the RT-Thread project root.  These bytes are placed in Flash. */
#include "probe_assets.h"
.section .rodata.tcm_probe,"a",%progbits
.balign 32
.global tcm_probe_model
.type tcm_probe_model,%object
tcm_probe_model:
.incbin "applications/tcm_static_probe/model.tflite"
.size tcm_probe_model, .-tcm_probe_model

#if TCM_SAMPLE_COUNT == 1
.balign 32
.global tcm_probe_input
.type tcm_probe_input,%object
tcm_probe_input:
.incbin "applications/tcm_static_probe/samples.bin"
.size tcm_probe_input, .-tcm_probe_input
#endif
