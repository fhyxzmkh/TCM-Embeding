/* Entry point for the separate RT-Thread CM55 static probe project. */
extern int tcm_run_probe(void);

int main(void)
{
    return tcm_run_probe();
}
