#ifndef TCM_HOME_ATLAS_H
#define TCM_HOME_ATLAS_H

#include <stdint.h>

typedef struct {
    uint32_t code, name, notes, modern;
    uint16_t category, source_omitted;
} home_atlas_entry_t;

extern const char home_atlas_blob[];
extern const home_atlas_entry_t home_atlas_entries[];
extern const uint32_t home_atlas_categories[];
extern const unsigned home_atlas_entry_count, home_atlas_category_count;
extern const unsigned home_atlas_blob_size;
extern const uint32_t home_atlas_blob_crc;

void home_atlas_create(void *screen, void *header);

#endif
