import hashlib, time, json
from argparse import ArgumentParser as ap

def do_stream(input_file, config):
    md5 = hashlib.md5()

    chunksize = config['width']
    downtime = config['downtime']
    maxbytes = config.get('maxbytes', None)

    read_limit = 2**31 - 1
    if maxbytes is not None and maxbytes > read_limit:
        print(f'Setting maxbytes (requested:{maxbytes}) to 2GB read limit')
        maxbytes = read_limit

    filesize = input_file.seek(0, 2) #This returns the size of the file
    input_file.seek(0) #Go back to start
    if input_file.tell() != 0:
        raise Exception('Could not get back to the start')

    total = 0
    chunk_sizes = []
    chunk_times_us = []
    wall_start = time.perf_counter_ns()
    while True:
        # Time only the read() call itself
        t0 = time.perf_counter_ns()
        chunk = input_file.read(chunksize)
        t1 = time.perf_counter_ns()
        if not chunk:
            break

        chunk_sizes.append(len(chunk))
        chunk_times_us.append((t1 - t0) / 1e3)

        if config['checksum']: md5.update(chunk)
        total += len(chunk)

        if maxbytes and total >= maxbytes: break
        if downtime > 0: time.sleep(downtime)
    wall_ms = (time.perf_counter_ns() - wall_start) / 1e6

    results = dict(
        filesize=filesize,
        md5=(md5.hexdigest() if config['checksum'] else None),
        streamed_size=total,
        read_time_ms=sum(chunk_times_us) / 1e3,  # time blocked in read() only
        wall_time_ms=wall_ms,                    # includes processing + sleep
        chunk_sizes=chunk_sizes,
        chunk_times_us=chunk_times_us,
    )

    return results


if __name__ == '__main__':
    parser = ap()
    parser.add_argument('input', type=str, help='Input file or pfn to stream in')
    # parser.add_argument(
    #     '-c', '--config',
    #     type=str, help='yaml file to configure the stream testing',
    #     required=True)
    parser.add_argument(
        '--dry-run',
        action='store_true', help='Dry run. Dump some config info'
    )
    parser.add_argument(
        '--verbose',
        action='store_true'
    )
    parser.add_argument(
        '-o', '--output',
        type=str, help='Name of output json file to store some info'
    )

    parser.add_argument(
        '--stream-maxbytes', type=int,
        default=None,
    )
    parser.add_argument(
        '--stream-width', type=int,
        default=8192
    )
    parser.add_argument(
        '--stream-downtime', type=float,
        default=1.0
    )
    parser.add_argument(
        '--checksum', action='store_true',
        help='Run checksum'
    )

    args = parser.parse_args()

    # with open(args.config, "r") as cfile:
    #     config = yaml.safe_load(cfile)
    # if args.verbose:
    #     print(config)
    config = dict()
    config['maxbytes'] = args.stream_maxbytes
    config['width'] = args.stream_width
    config['downtime'] = args.stream_downtime
    config['checksum'] = args.checksum

    output_dict = dict()
    
    # Unbuffered so each read() is one request to the underlying (XRootD) layer
    with open(args.input, 'rb', buffering=0) as input_file:
        output_dict['stream'] = do_stream(input_file, config)
    
    if args.output:
        with open(args.output, 'w') as output_file:
            json.dump(output_dict, output_file, indent=4)