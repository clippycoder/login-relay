// SPDX-License-Identifier: GPL-3.0-or-later
using Xunit;

// Several tests start servers on thread-pool threads and measure short real-time windows (the 400 ms
// "does the connection stay open" rule, bounded waits for process output). Run in parallel on a small
// CI machine, they starve each other of pool threads and fail for reasons that have nothing to do with
// the code under test.
[assembly: CollectionBehavior(DisableTestParallelization = true)]
