# Build the jobchain compute-node helper.
#
#   make            optimized build into bin/jobchain-node
#   make debug      same sources with AddressSanitizer and UndefinedBehaviorSanitizer
#   make static     statically linked, for clusters whose nodes differ from the
#                   submit host
#   make clean      remove build products

CC      ?= cc
CFLAGS  ?= -O2 -std=c99 -pedantic -Wall -Wextra -Werror \
           -Wshadow -Wconversion -Wstrict-prototypes -Wmissing-prototypes
LDFLAGS ?=

SRC     := src/jobchain-node.c

BIN     := bin/jobchain-node

.PHONY: all debug static clean mutation state-properties concurrency load bottlenecks

all: $(BIN)

$(BIN): $(SRC)
	@mkdir -p bin
	$(CC) $(CFLAGS) -o $@ $(SRC) $(LDFLAGS)

# Sanitizer build. The test suite runs against this binary so that use of
# uninitialized memory or an out-of-bounds path buffer fails the build rather
# than surfacing as a rare mid-run fault on a compute node.
debug: $(SRC)
	@mkdir -p bin
	$(CC) -O1 -g -std=c99 -pedantic -Wall -Wextra -Werror \
	      -fsanitize=address,undefined -fno-omit-frame-pointer \
	      -o $(BIN) $(SRC)

# Static linking removes the runtime dependency on the submit host's libc,
# which matters when compute nodes run a different image.
static: $(SRC)
	@mkdir -p bin
	$(CC) $(CFLAGS) -static -o $(BIN) $(SRC)

mutation:
	./mutation_tests/run.py

clean:
	rm -f $(BIN)
	rm -rf bin/*.dSYM

state-properties:
	python3 state_property_tests/run.py

concurrency:
	python3 concurrency_tests/run.py

load:
	python3 load_tests/run.py

bottlenecks:
	python3 bottleneck_tests/run.py
