# AUTO replay recovery — extension 0.2.8

Extension 0.2.8 replaces permanent pre-execution replay timestamps with a bounded claim lifecycle.

A sequential AUTO iteration receives a processing claim with a 180-second lease. The lease exceeds the extension's bounded initial wait, result polling, and promotion-observation window, so a live action cannot be reclaimed early. Duplicate live panels report iteration_in_progress and wait through the bounded retry loop. Successful execution advances canonical state before the claim is marked completed. Exceptions release the processing claim, while abandoned or legacy timestamp claims can be reclaimed after the lease. An iteration already present in canonical state is reported as iteration_already_processed and is never executed again.

The runtime acceptance covers recovery after a Native Messaging exception, successful retry of the same final iteration, one in-flight execution across duplicate panels, post-completion deduplication, and completion of the configured final iteration without requesting another continuation.

Replay protection remains global for each loop and iteration. Opt-in, terminal states, promotion checks, and Native Host policy remain unchanged; milestone AUTO has no total iteration or duration ceiling.
