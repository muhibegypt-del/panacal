#!/usr/bin/perl
# Test double for pgenerator-lg: forwards the helper request to the simulated
# LG TV (tests/sim/sim_tv.py) and prints its JSON answer, exactly as the real
# helper prints its result.
use strict;
use warnings;
use IO::Socket::INET;
use MIME::Base64 qw(decode_base64);

my $body=decode_base64($ENV{"PGEN_LG_REQUEST_B64"}||"");
my $sock=IO::Socket::INET->new(PeerHost=>"127.0.0.1",PeerPort=>$ENV{"PGEN_SIM_PORT"},Proto=>"tcp",Timeout=>10)
 or do { print '{"status":"error","message":"simulated TV is not running"}'; exit 1; };
binmode($sock);
print $sock "POST /helper HTTP/1.0\r\nContent-Type: application/json\r\nContent-Length: ".length($body)."\r\n\r\n".$body;
local $/;
my $raw=<$sock>;
close($sock);
my (undef,$content)=split(/\r?\n\r?\n/,$raw,2);
print $content;
