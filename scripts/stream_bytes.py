import hashlib, time, json
from argparse import ArgumentParser as ap


def do_stream(input_file, config):
    md5 = hashlib.md5()

    chunksize = config['stream']['width']
    downtime = config['stream']['downtime']
    maxbytes = config['stream'].get('maxbytes', None)

    filesize = input_file.seek(0, 2) #This returns the size of the file
    input_file.seek(0) #Go back to start
    if input_file.tell() != 0:
        raise Exception('Could not get back to the start')
    
    total = 0
    while (chunk := input_file.read(chunksize)):
        md5.update(chunk)
        total += chunksize
        if maxbytes and total > maxbytes: break
        time.sleep(downtime)

    results = dict(
        filesize=filesize,
        md5=md5.hexdigest(),
    )

    return results


if __name__ == '__main__':
    parser = ap()
    parser.add_argument('input', type=str, help='Input file or pfn to stream in')
    parser.add_argument(
        '-c', '--config',
        type=str, help='yaml file to configure the stream testing',
        required=True)
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

    args = parser.parse_args()

    # with open(args.config, "r") as cfile:
    #     config = yaml.safe_load(cfile)
    # if args.verbose:
    #     print(config)
    config = dict()
    config['stream'] = dict()
    config['stream']['maxbytes'] = args.stream_maxbytes
    config['stream']['width'] = args.stream_width
    config['stream']['downtime'] = args.stream_downtime

    output_dict = dict()
    
    with open(args.input, 'rb') as input_file:
        output_dict['stream'] = do_stream(input_file, config)
    
    if args.output:
        with open(args.output, 'w') as output_file:
            json.dump(output_dict, output_file, indent=4)