import pytest
from brownie import GTA, TokenDistributor, Timelock, accounts, web3, Wei, reverts
import time
import requests
import json
import hmac
import hashlib
import binascii
import csv
import random

v1_api_uri = 'https://esms-audit.grasshopper.surf/v1/sign_claim'
dev_hmac_key = 'E49756B4C8FAB4E48222A3E7F3B97CC3' 
signing_address = '0x58E159e41bA3987755fF762836CC7338C0bC01ef' # dev/testing
# merkleRoot = '0x7fbcd210e229bbea2fd3e2e70fec625b962180383f387c8427b7ec8aa3aad431' # initial dist sample from KO v1 4/13/2021
merkleRoot = '0xae2d57be918fb3faf024c55c14461753fd241ed523b61e0ac60aa5408c39066a' # new version of merkle tree with KO v1 initial dist 4/14/21
timelock_delay = 172800 # 2 days in seconds
test_dist_file = './tests/initial_dist.csv' # initial dist sample from KO v1 4/13/2021

@pytest.fixture(scope="module")
def token():
    """ 
        Deploy the token contract w/three params:
        multiSig - The initial account to grant all the tokens
        minter_ - The account with minting ability 
        mintingAllowedAfter_ - The timestamp after which minting may occur (unix time)
    """ 
    multiSig = accounts[0]
    minter = accounts[0]
    mintingAllowedAfter = int(time.time()) 
    return GTA.deploy(multiSig, minter, mintingAllowedAfter, {'from': accounts[0]})

@pytest.fixture(scope="module")
def tl():
    """
        TimeLock Contract - Only needed here in the TD test as all GTC not claimed 
        can be swept to TimeLock after 6 months
    """
    multiSig = accounts[0] 
    return Timelock.deploy(multiSig, timelock_delay, {'from': accounts[0]})

@pytest.fixture(scope="module")
def td(token, tl):
    """ 
        TokenDistributor.sol constructor params:
        <constructor(address _token, address _signer, address _timeLock, bytes32 _merkleRoot)> 
        _token - ERC20 token that will be distributed  
        _signer - pub-key/address of the account used to sign token claims
        _timeLock - Address for the TimeLock contract
        _merkleRoot - Merkle Root of the distribution tree 
    """
    _token = token.address
    return TokenDistributor.deploy(_token, signing_address, tl.address, merkleRoot, {'from': accounts[0]})

@pytest.fixture(scope="module")
def set_dist_address(token, td):
    """Token needs to know the tokenDist contract address for approved setting of delegate with different source address"""
    return token.setGTCDist(td.address, {'from': accounts[0]}) 

@pytest.fixture(scope="module")
def seed(token, td):
    """Tansfer seed tokens to the distributor contract"""
    return token.transfer(td.address, Wei('1000000 ether'), {'from': accounts[0]})

@pytest.fixture(autouse=True)
def isolation(fn_isolation):
    """snapshot/isolate the env after above fixtures so the tests below run against a clean snapshot"""
    pass

def test_valid_contract_address(token, td):
    """generic test to confirm we have a working contract address"""
    assert web3.isChecksumAddress(token.address) and web3.isChecksumAddress(token.address), "One or more contract addresses could not be validated. Please confirm contracts we're deployed as expected."
 
def test_dist_address_on_token(token, td):
    token.setGTCDist(td.address, {'from': accounts[0]})
    assert token.GTCDist() != '0x0000000000000000000000000000000000000000', "Token doesn't have the TokenDistribution contract address set appropriately for delegation on dist."

def test_valid_claim(token,td,seed,set_dist_address): 
    '''
       Submit claim to ESMS use respone to make on-chain claim.
       Test that a valid claim will transfer tokens to user  
    '''
    # valid claim from 4/13 merkle root 
    claim_address = accounts[0].address
    delegate_address = accounts[1].address
    user_id = 3221 
    total_claim = 8007641666299999993856
    
    token_claim = TokenClaim(user_id, claim_address, delegate_address, total_claim) 
    
    # get use balance before claim 
    balance_before = token.balanceOf(claim_address)

    # place token claim 
    td.claimTokens(token_claim.user_id, token_claim.user_address, token_claim.user_amount, token_claim.delegate_address, token_claim.hash, token_claim.sig, token_claim.proof, token_claim.leaf)

    # get use balance before claim 
    balance_after = token.balanceOf(claim_address)

    print(f'balance_before: {balance_before}')
    print(f'balance_after: {balance_after}')

    assert balance_before < balance_after, "Tokens do not appear to have been sent"
    # uncomment to debug and print details to stdout 
    # assert False, "You intentionally triggered execpetion to print debug info to stdout"


def test_full_dist_list(token, td, seed, set_dist_address):
    """Iterate though and test every claim on the list"""
  
    with open(test_dist_file, 'r') as csvfile:
        initial_distribution = csv.reader(csvfile)
        next(initial_distribution) # skip header 
        
        for row in initial_distribution:
            random_index = random.randint(0, 9) # pick a random number for address index 
            user_id = int(row[1]) # user_id 
            total_claim = int(row[2]) # total_claim
            claim_address = accounts[random_index].address # set random address 
            delegate_address = claim_address # self delegate 
                   
            # get balance before  
            balance_before = token.balanceOf(claim_address)
            
            # craft claim object 
            token_claim = TokenClaim(user_id, claim_address, delegate_address, total_claim)
            
            # make claim
            try: 
                claim_tx = td.claimTokens(token_claim.user_id, token_claim.user_address, token_claim.user_amount, token_claim.delegate_address, token_claim.hash, token_claim.sig, token_claim.proof, token_claim.leaf, {'from' : claim_address})
            except Exception as e:
                print(f'TokenDistribution test: There was an issue sending claim to the contract: {e}') 
            
            # get use balance before claim 
            balance_after = token.balanceOf(claim_address)

            # print(f'balance_before: {balance_before}')
            # print(f'balance_after: {balance_after}')
            # print(f'gas_used: {claim_tx.gas_used}')
            assert balance_before < balance_after, "Token claim failed"
             
    # uncomment to debug and print details to stdout 
    # assert False, "You intentionally triggered execpetion to print debug info to stdout"

# for crafting token claim objects  
class TokenClaim:

  def __init__(self, _user_id, _user_address, _delegate_address, _total_claim):
      ''' push claim objects emitted from Ethereum Message
          Signing Service into an on-chain claimable object 
      '''
      raw_claim = generate_claim(_user_id, _user_address, _delegate_address, _total_claim)
     
      self.user_id = _user_id
      self.user_address = _user_address 
      self.user_amount = _total_claim
      self.delegate_address = _delegate_address
      self.hash = raw_claim["eth_signed_message_hash_hex"]
      self.sig = raw_claim["eth_signed_signature_hex"]
      self.leaf = raw_claim["leaf"]
      self.proof = raw_claim["proof"]


def generate_claim(user_id, user_address, delegate_address, total_claim):
    '''Mimic Quadratic Lands application by sending a claim request to the Ethereum Signed Message Service'''

    post_data_to_emss = {}
    post_data_to_emss['user_id'] = user_id
    post_data_to_emss['user_address'] = user_address
    post_data_to_emss['delegate_address'] = delegate_address
    post_data_to_emss['user_amount'] = total_claim

    # print(f'POST DATA FOR ESMS: {json.dumps(post_data_to_emss)}')
    # create a hash of post data
    try:                 
        hmac_signed_claim = create_sha256_signature(dev_hmac_key, json.dumps(post_data_to_emss))
    except: 
        print('Error creating hash of POST data for ESMS')

    header = { 
        "X-GITCOIN-SIG" : hmac_signed_claim,
        "content-type": "application/json",
    }

    # POST relevant user data to micro service that returns signed transation data for the user broadcast
    try: 
        emss_response = requests.post(v1_api_uri, data=json.dumps(post_data_to_emss), headers=header)
        emss_response_content = emss_response.content
        emss_response.raise_for_status() # raise exception on error 
    except requests.exceptions.ConnectionError:
        print('TokenDistribtor: ConnectionError while connecting to ESMS')
     
    except requests.exceptions.Timeout:
        # Maybe set up for a retry
        print('TokenDistribtor: Timeout while connecting to ESMS')
 
    except requests.exceptions.TooManyRedirects:
        print('TokenDistribtor: Too many redirects while connecting to ESMS')
     
    except requests.exceptions.RequestException as e:
        # catastrophic error. bail.
        print(f'TokenDistribtor test Error posting to ESMS - {e}')
    
    try:
        # ESMS returns may retrun objects. so, we decode 
        full_response = json.loads(emss_response_content.decode('utf-8'))
    except Exception as e:
        full_response = []
        print(f'TokenDistribution test Error - {e}')

    # print(f'GTC Token Distributor - ESMS response: {full_response}')
    return full_response 
    

def create_sha256_signature(key, message):
    '''Given key & message, returns HMAC digest of the message'''
    try:
        byte_key = binascii.unhexlify(key)
        message = message.encode()
        return hmac.new(byte_key, message, hashlib.sha256).hexdigest().upper()
    except Exception as e:
        logger.error(f'TokenDistribtor - Error Hashing Message: {e}')
        return False 


